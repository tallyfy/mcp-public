"""Response sanitizer for user-visible MCP tool output (issue #170).

The MCP server returns ``ToolResult`` objects whose ``content`` is rendered
to the LLM as JSON. The LLM then surfaces that JSON, often verbatim, to
the end user. Internal-only metadata leaks through:

* canonical ``field_type`` codes (``assignees_form``, ``textarea``) that
  mean nothing to a user;
* 32-char hex IDs under non-id keys (``stored_as``, ``field_type_internal``,
  internal alias hashes) that look like noise to a user;
* alias-resolution debug strings ("Field Type Sent: assignee_picker, Stored
  As: assignees_form").

This module provides two pure helpers and a thin wrapper:

* :func:`display_name_for_field_type` — map a canonical type code to a
  friendly label (e.g. ``"assignees_form"`` → ``"Assignee Picker"``).
* :func:`strip_internal_metadata` — recursively walk a payload, drop
  internal-only keys, drop free-floating 32-char hex strings under
  non-chaining keys, and translate canonical ``field_type`` values via
  the display-name table.
* :func:`sanitize_for_user_text` — convenience wrapper that applies
  ``strip_internal_metadata`` and is the single function tools should
  call when shaping the user-visible content payload.

**Tool chaining is preserved**: the IDs the LLM needs for follow-up calls
(``id``, ``run_id``, ``task_id``, anything ending in ``_id`` / ``_ids``)
are kept untouched. Only loose hex values under non-chaining keys are
stripped.

Pure / referentially transparent — no I/O, no side effects, no shared
state. Always returns a fresh structure (never mutates the input).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Display-name table — canonical internal field_type → user-friendly label.
# ---------------------------------------------------------------------------
#
# Rows 1-10: form_fields.py canonical types (the ten the API allows in
#            CaptureRequestValidator).
# Rows 11+ : user_interaction.py field types (UI form components used by
#            ``ask_user_question`` — these aren't backend captures, but the
#            same display rules apply when surfaced to the user).
#
# Forward-compat: any canonical type missing from this table will be returned
# unchanged by :func:`display_name_for_field_type` (no crash). Adding a row
# is the only step required to humanise a new type code.

INTERNAL_FIELD_TYPE_DISPLAY_NAMES: dict[str, str] = {
    # --- form_fields.py canonical API types --------------------------
    "text": "Short Text",
    "textarea": "Large Text",
    "email": "Email",
    "date": "Date",
    "dropdown": "Dropdown",
    "multiselect": "Multi-Select",
    "radio": "Multiple Choice",
    "file": "File Upload",
    "table": "Table",
    "assignees_form": "Assignee Picker",
    # --- user_interaction.py UI types --------------------------------
    "select": "Dropdown",
    "checkbox": "Checkbox",
    "toggle": "Toggle",
    "datepicker": "Date",
    "daterange": "Date Range",
    "timepicker": "Time",
    "datetime": "Date & Time",
    # 'radio' and 'file' already covered above.
}


# ---------------------------------------------------------------------------
# Constants for hex-ID detection / chaining-key preservation.
# ---------------------------------------------------------------------------

# A "loose" 32-char hex string we consider an internal ID candidate.
# fullmatch — must be the entire string, not embedded in prose.
_HEX32_RE = re.compile(r"[0-9a-f]{32}")

# Keys whose VALUES we always preserve verbatim — even if they look like
# internal hex IDs. These are needed by the LLM for follow-up tool calls
# (chaining). The check is suffix-based ("anything ending in _id") plus
# an explicit allow-list for unsuffixed keys like ``id``.
_CHAINING_KEY_SUFFIXES: Tuple[str, ...] = ("_id", "_ids")
_CHAINING_KEY_ALLOWLIST: frozenset[str] = frozenset({
    "id",
    "uuid",
})

# Keys whose VALUES we always drop entirely — they leak alias-resolution
# / debug detail that has no value to a user.
_INTERNAL_ONLY_KEYS: frozenset[str] = frozenset({
    "field_type_internal",
    "stored_as",
    "kickoff_form_field_internal_alias",
    "internal_org_hash",
    "alias_resolved_from",
    "_internal_alias_chain",
})

# Keys whose VALUE is a canonical field_type code that we should translate
# via the display-name table. Anywhere these keys appear, we map values.
_FIELD_TYPE_KEYS: frozenset[str] = frozenset({
    "field_type",
    # Note: 'type' is intentionally NOT here — too generic; might reference
    # totally unrelated things (search result type, automation type, etc.).
})


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def display_name_for_field_type(internal_type: Optional[str]) -> Optional[str]:
    """Return a user-friendly display name for a canonical ``field_type``.

    Parameters
    ----------
    internal_type
        The canonical internal type code, e.g. ``"assignees_form"``,
        ``"textarea"``, ``"datetime"``. Empty string and ``None`` are
        accepted and round-tripped unchanged so callers don't need to
        guard against them.

    Returns
    -------
    The friendly display string from
    :data:`INTERNAL_FIELD_TYPE_DISPLAY_NAMES`, or the input unchanged
    if no mapping is defined (forward-compat).

    Examples
    --------
    >>> display_name_for_field_type("assignees_form")
    'Assignee Picker'
    >>> display_name_for_field_type("textarea")
    'Large Text'
    >>> display_name_for_field_type("brand_new_thing")
    'brand_new_thing'
    """
    if internal_type is None:
        return None
    if not internal_type:
        return internal_type
    return INTERNAL_FIELD_TYPE_DISPLAY_NAMES.get(internal_type, internal_type)


def _is_chaining_key(key: str) -> bool:
    """Return True if values under ``key`` should be preserved verbatim.

    Chaining keys carry IDs the LLM needs for follow-up tool calls.
    """
    if not isinstance(key, str):
        return False
    if key in _CHAINING_KEY_ALLOWLIST:
        return True
    return any(key.endswith(suf) for suf in _CHAINING_KEY_SUFFIXES)


def _is_loose_hex32(value: Any) -> bool:
    """True if ``value`` is a string that is **exactly** a 32-char lower hex.

    Strings that contain a 32-char hex inside a longer message are NOT
    matched — only fullmatch — so prose / comment bodies survive.
    """
    return isinstance(value, str) and bool(_HEX32_RE.fullmatch(value))


def strip_internal_metadata(
    payload: Any,
    *,
    chaining_key_suffixes: Iterable[str] = _CHAINING_KEY_SUFFIXES,
    chaining_key_allowlist: Iterable[str] = _CHAINING_KEY_ALLOWLIST,
    internal_only_keys: Iterable[str] = _INTERNAL_ONLY_KEYS,
    field_type_keys: Iterable[str] = _FIELD_TYPE_KEYS,
) -> Any:
    """Recursively strip internal metadata from a tool-response payload.

    The walk applies four rules at every dict level:

    1. Drop keys in ``internal_only_keys`` outright.
    2. Translate values of ``field_type_keys`` via the display-name table.
    3. Drop keys whose VALUE is a loose 32-char hex string and whose name
       is *not* a chaining key (suffix ``_id`` / ``_ids``, or in the
       allow-list).
    4. Recurse into dict / list children.

    The function returns a NEW payload — the input is never mutated.

    Parameters
    ----------
    payload
        Any JSON-serialisable Python value (dict, list, scalar, None).

    Other parameters override the module-level defaults — useful for
    tests but rarely needed in production code.
    """
    chaining_suffixes = tuple(chaining_key_suffixes)
    chaining_allow = frozenset(chaining_key_allowlist)
    internal_keys = frozenset(internal_only_keys)
    type_keys = frozenset(field_type_keys)

    def _is_chain(k: str) -> bool:
        if not isinstance(k, str):
            return False
        if k in chaining_allow:
            return True
        return any(k.endswith(s) for s in chaining_suffixes)

    def _walk(node: Any) -> Any:
        if node is None:
            return None
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for key, value in node.items():
                # Rule 1 — drop internal-only keys.
                if key in internal_keys:
                    continue
                # Rule 3 — drop loose hex32 values under non-chaining keys.
                if _is_loose_hex32(value) and not _is_chain(key):
                    continue
                # Rule 2 — translate canonical field_type values.
                if key in type_keys and isinstance(value, str):
                    out[key] = display_name_for_field_type(value)
                    continue
                # Rule 4 — recurse into structured children.
                out[key] = _walk(value)
            return out
        if isinstance(node, list):
            return [_walk(item) for item in node]
        # tuples → lists for JSON safety; unlikely in practice.
        if isinstance(node, tuple):
            return [_walk(item) for item in node]
        # All other scalars (str, int, float, bool) pass through verbatim.
        # NOTE: a top-level loose hex32 scalar is preserved — the strip
        # rule fires only when there's a key context (so chaining keys
        # can be recognised).
        return node

    return _walk(payload)


def sanitize_for_user_text(payload: Any) -> Any:
    """Sanitize ``payload`` for the user-visible-text rendering path.

    Thin wrapper around :func:`strip_internal_metadata` so callers don't
    need to know the keyword-argument plumbing. Use this from
    ``utils.sdk_serializer`` (or any tool that wants explicit control
    over the final user-visible shape) to produce a payload safe to drop
    into ``ToolResult.content``.

    Tool chaining stays intact: every chaining-key (``id``, ``*_id``,
    ``*_ids``) survives. Only internal-only metadata is removed.
    """
    return strip_internal_metadata(payload)


__all__ = [
    "INTERNAL_FIELD_TYPE_DISPLAY_NAMES",
    "display_name_for_field_type",
    "strip_internal_metadata",
    "sanitize_for_user_text",
]
