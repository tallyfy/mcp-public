"""
Kickoff-data encoding helpers for process launch.

`prerun` and `roles` on POST /runs are OBJECTS keyed by ID — kickoff-field
`timeline_id` for `prerun`, org-role ID for `roles`:

    {"prerun": {"<timeline_id>": "value", ...}}

api-v2 reads them via Laravel dot-notation on the ID
(`RunRequestValidator.php:22` — `$this->get('prerun.'.$data['timeline_id'])`)
and iterates them keyed (`:33` — `foreach ($prerun_data as $prerunID => $values)`),
so a JSON list yields integer keys 0,1,2 that never match an ID. Every value is
silently dropped, and any required field then fails with "<label> is required".

The tool schema used to declare these as lists, so callers built list payloads.
The helpers below accept that legacy shape and convert it, which keeps existing
integrations working without a client-side change.

Note the name collision that caused the original bug: on a TEMPLATE
(`PUT /checklists/{id}`) `prerun` legitimately IS a list of field *definitions*.
These helpers are only for the run-launch value payload.
"""

from typing import Any, Dict, Optional, Union

from fastmcp.exceptions import ToolError


def _merge_legacy_list(value: list, param: str) -> Dict[str, Any]:
    """Fold a legacy ``[{"<id>": v}, ...]`` list into a single keyed object."""
    merged: Dict[str, Any] = {}
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ToolError(
                f"{param} must be an object keyed by "
                f"{'kickoff field timeline_id' if param == 'prerun' else 'org role ID'}, "
                f'for example {param}={{"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6": "value"}}. '
                f"Received a list whose item at position {index} is "
                f"{type(entry).__name__}, not an object."
            )
        for key, item in entry.items():
            merged[str(key)] = item
    return merged


def normalize_keyed_payload(
    value: Optional[Union[Dict[str, Any], list]],
    param: str,
) -> Optional[Dict[str, Any]]:
    """
    Coerce ``prerun``/``roles`` into the ID-keyed object the API requires.

    Accepts the correct object form unchanged, folds the legacy
    ``[{"<id>": value}, ...]`` list form into one object, and raises a
    ToolError naming the expected shape for anything else.
    """
    if value is None:
        return None

    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}

    if isinstance(value, list):
        if not value:
            return {}
        return _merge_legacy_list(value, param)

    raise ToolError(
        f"{param} must be an object keyed by "
        f"{'kickoff field timeline_id' if param == 'prerun' else 'org role ID'}, "
        f'for example {param}={{"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6": "value"}}. '
        f"Received {type(value).__name__}."
    )
