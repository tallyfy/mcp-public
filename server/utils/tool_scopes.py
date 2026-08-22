"""Per-tool ``mcp_scopes`` requirements, and the decision function over them.

Why this exists (tallyfy/mcp#559)
---------------------------------
An MCP access token carries an ``mcp_scopes`` claim naming exactly what the user
approved on the consent screen. Until this module existed, ``rg mcp_scopes
server/`` returned two hits and both were COMMENTS: nothing on this server read
the claim, so a token approved for only "View your tasks" could call
``delete_template`` and every other write tool we expose. api-v2's
``EnforceMcpTokenScopes`` middleware gates the REST API, so the data was not
unprotected -- but this server was contributing nothing to that protection, and a
tool that maps to an endpoint api-v2 leaves unmapped had no gate at all.

This is defense in depth, deliberately mirroring api-v2 rather than inventing a
second policy. The scope vocabulary is imported from ``constants.MCPScopes``,
which is already the wire-value list this server publishes in its OAuth discovery
documents, and which matches ``McpAccessTokenService::SUPPORTED_SCOPES`` in
api-v2 one-for-one: six families times read/write.

THE ONE RULE THAT MUST NOT BE BROKEN
------------------------------------
**A token carrying no ``mcp_scopes`` claim is not gated at all.** api-v2's
middleware returns early for exactly that case ("It ONLY affects MCP-issued
tokens -- a request whose bearer JWT has no mcp_scopes claim (ordinary session /
API tokens) is passed through untouched"), and the reason is not theoretical:
chat.tallyfy.com and the desktop native AI shell forward the user's RAW Tallyfy
session token to this server, not an OAuth-minted one. A gate that failed closed
on an absent claim would 403 every tool call from Tallyfy's own products. See
tallyfy/mcp#588, 2026-07-15 comment, section 1.

Fail-closed applies ONLY to a token that HAS the claim: an unmapped tool, or a
tool whose required scope is not in the granted set.

Read/write, and why the map is not free-form
--------------------------------------------
api-v2 infers the action from the HTTP method (GET/HEAD/OPTIONS read;
PUT/PATCH/DELETE write; POST write unless a read-only suffix such as ``search``
or ``export``). This server has no HTTP method to read at the tool boundary, so
the action is written into the map -- and pinned to an INDEPENDENT source:
``tests/unit/server/utils/test_tool_scopes.py`` fails if a tool declaring
``readOnlyHint=True`` is mapped to anything but ``.read`` scopes, or if a tool
declaring ``readOnlyHint=False`` requires no ``.write`` scope at all. Those hints
are already mandatory on every tool and already independently guarded, so the
map cannot silently drift into calling a delete tool read-only.

**A write scope satisfies a read requirement** for the same family, exactly as
``EnforceMcpTokenScopes::scopeSatisfied`` does. The converse is never true.

Requirements are a FROZENSET and ALL members must be satisfied
--------------------------------------------------------------
101 of the 103 mapped tools need exactly one scope. The set exists for the three
that legitimately span families, where picking one would under-gate:

* ``search_all`` returns tasks, processes and templates in one response.
* ``tallyfy_api_read`` / ``tallyfy_api_write`` are the universal escape hatch and
  can reach ANY Tallyfy endpoint. If these were reachable on one narrow scope,
  the whole map would be decoration. They require every read (resp. every write)
  scope. They are additionally gated OFF by ``MCP_ENABLE_API_FALLBACK`` and by
  ``utils.tallyfy_endpoint_allowlist``.

Judgement calls, stated rather than buried
------------------------------------------
* **Tags and folders have no family in the scope vocabulary.** api-v2's
  ``RESOURCE_MAP`` has no entry for either, so it leaves both ungated. Tagging or
  foldering a specific object is mapped to that object's family
  (``tag_process`` -> processes, ``get_template_folders`` -> templates). The
  org-level container CRUD (``create_tag``, ``create_folder``,
  ``add_object_to_folder``, ...) is mapped to the **templates** family: it is
  library-organisation work, and templates is the surface a client doing it
  holds. This is STRICTER than api-v2, which is the safe direction, but it does
  mean a processes-only token can no longer create a tag. ``log`` mode exists to
  measure that before anyone enforces it in a new environment.
* **Kickoff fields are the forms family, not templates.** api-v2 maps
  ``kickoff-form-fields`` -> forms, so ``get_kickoff_fields`` and
  ``suggest_kickoff_fields`` (which live in ``tools/template_management.py``) sit
  with their siblings in ``tools/form_fields.py``.
* **Kickoff form COMPLETION is the processes family.** ``complete_kickoff_form``
  and ``reopen_kickoff_form`` both take a ``run_id`` and write to that run.

Exempt tools
------------
``EXEMPT_TOOLS`` is not an escape hatch for anything awkward. It holds only tools
that reach no Tallyfy resource family at all, plus the two identity/utility reads
api-v2's own middleware deliberately leaves unmapped so it "never breaks
identity/utility endpoints". Every entry carries its reason, and the completeness
test requires an entry in exactly one of the two tables.

Related, and FIXED SEPARATELY in #856: ``utils.tallyfy_endpoint_allowlist``'s
write gate was fed by ``auth_context.get_jwt_scopes()``, which reads
``AccessToken.scopes``. fastmcp populates that from the ``scope`` / ``scp``
claims, and a Tallyfy MCP token carries neither -- its permissions are in
``mcp_scopes``. That is why this module reads the claim itself via
``auth_context.get_mcp_scopes()``, and it is now what that gate reads too.

**The two gates agree on the absent claim, and that agreement is deliberate.**
``decide`` below returns ``no_mcp_scopes_claim`` / allowed for ``granted is
None``; ``tallyfy_endpoint_allowlist.check`` does the same for
``mcp_scopes=None``. Before #856 they would have disagreed -- this one passing
a claimless first-party token through, that one refusing it -- which is a
worse outcome than either policy alone. If you change the rule here, change it
there in the same commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Optional

from constants import MCPScopes

# The complete wire vocabulary, derived from the class rather than re-listed, so
# a scope added to MCPScopes cannot be missing here. Mirrors api-v2's
# McpAccessTokenService::SUPPORTED_SCOPES.
MCP_SCOPE_VOCABULARY: FrozenSet[str] = frozenset(
    value
    for name, value in vars(MCPScopes).items()
    if not name.startswith("_") and isinstance(value, str)
)

READ_SUFFIX = ".read"
WRITE_SUFFIX = ".write"

# Shorthands, purely to keep the table below readable.
_USERS_R = frozenset({MCPScopes.USERS_READ})
_USERS_W = frozenset({MCPScopes.USERS_WRITE})
_TASKS_R = frozenset({MCPScopes.TASKS_READ})
_TASKS_W = frozenset({MCPScopes.TASKS_WRITE})
_PROC_R = frozenset({MCPScopes.PROCESSES_READ})
_PROC_W = frozenset({MCPScopes.PROCESSES_WRITE})
_TMPL_R = frozenset({MCPScopes.TEMPLATES_READ})
_TMPL_W = frozenset({MCPScopes.TEMPLATES_WRITE})
_FORM_R = frozenset({MCPScopes.FORMS_READ})
_FORM_W = frozenset({MCPScopes.FORMS_WRITE})
_AUTO_R = frozenset({MCPScopes.AUTOMATION_READ})
_AUTO_W = frozenset({MCPScopes.AUTOMATION_WRITE})

ALL_READ_SCOPES: FrozenSet[str] = frozenset(
    s for s in MCP_SCOPE_VOCABULARY if s.endswith(READ_SUFFIX)
)
ALL_WRITE_SCOPES: FrozenSet[str] = frozenset(
    s for s in MCP_SCOPE_VOCABULARY if s.endswith(WRITE_SUFFIX)
)

# Every registered tool -> the scopes a scoped token must ALL hold to call it.
TOOL_SCOPES: Dict[str, FrozenSet[str]] = {
    # -- users --------------------------------------------------------------
    "get_guest": _USERS_R,
    "get_organization_users": _USERS_R,
    "get_organization_users_list": _USERS_R,
    "get_user": _USERS_R,
    "get_group": _USERS_R,
    "get_groups": _USERS_R,
    "get_organization_guests": _USERS_R,
    "get_organization_guests_list": _USERS_R,
    "change_user_role": _USERS_W,
    "create_guest": _USERS_W,
    "disable_guest": _USERS_W,
    "disable_user": _USERS_W,
    "enable_guest": _USERS_W,
    "enable_user": _USERS_W,
    "invite_user_to_organization": _USERS_W,
    "update_guest": _USERS_W,
    "create_group": _USERS_W,
    "delete_group": _USERS_W,
    "update_group": _USERS_W,
    # -- tasks --------------------------------------------------------------
    "get_guest_tasks": _TASKS_R,
    "get_my_tasks": _TASKS_R,
    "get_standalone_task": _TASKS_R,
    "get_task": _TASKS_R,
    "get_tasks_for_process": _TASKS_R,
    "get_user_tasks": _TASKS_R,
    "get_task_comments": _TASKS_R,
    "search_for_tasks": _TASKS_R,
    "complete_task": _TASKS_W,
    "create_standalone_task": _TASKS_W,
    "reopen_task": _TASKS_W,
    "update_standalone_task": _TASKS_W,
    "update_task": _TASKS_W,
    "add_task_comment": _TASKS_W,
    "delete_task_comment": _TASKS_W,
    "report_task_issue": _TASKS_W,
    "resolve_task_issues": _TASKS_W,
    "update_task_comment": _TASKS_W,
    # -- processes (runs) ---------------------------------------------------
    "get_organization_runs": _PROC_R,
    "get_process": _PROC_R,
    "search_for_processes": _PROC_R,
    "archive_process": _PROC_W,
    "launch_process": _PROC_W,
    "reactivate_process": _PROC_W,
    "update_process": _PROC_W,
    # Both take a run_id and write to that run's kickoff answers.
    "complete_kickoff_form": _PROC_W,
    "reopen_kickoff_form": _PROC_W,
    "tag_process": _PROC_W,
    "untag_process": _PROC_W,
    # -- templates (blueprints/checklists) ----------------------------------
    "assess_template_health": _TMPL_R,
    "get_all_templates": _TMPL_R,
    "get_step_dependencies": _TMPL_R,
    "get_template": _TMPL_R,
    "get_template_steps": _TMPL_R,
    "suggest_step_deadline": _TMPL_R,
    "search_for_templates": _TMPL_R,
    # Snippets are reusable text templates.
    "search_for_snippets": _TMPL_R,
    "get_template_folders": _TMPL_R,
    "get_tags": _TMPL_R,
    "add_assignees_to_step": _TMPL_W,
    "add_step_to_template": _TMPL_W,
    "clone_step": _TMPL_W,
    "clone_template": _TMPL_W,
    "create_template": _TMPL_W,
    "delete_step": _TMPL_W,
    "delete_template": _TMPL_W,
    "edit_description_on_step": _TMPL_W,
    "reorder_step": _TMPL_W,
    "update_step": _TMPL_W,
    "update_template": _TMPL_W,
    "tag_template": _TMPL_W,
    "untag_template": _TMPL_W,
    # Org-level tag and folder containers -- see "Judgement calls" above.
    "create_tag": _TMPL_W,
    "update_tag": _TMPL_W,
    "delete_tag": _TMPL_W,
    "create_folder": _TMPL_W,
    "update_folder": _TMPL_W,
    "delete_folder": _TMPL_W,
    "add_object_to_folder": _TMPL_W,
    "remove_object_from_folder": _TMPL_W,
    # -- forms (form fields, kickoff fields) --------------------------------
    "get_dropdown_options": _FORM_R,
    "get_kickoff_dropdown_options": _FORM_R,
    "suggest_form_fields_for_step": _FORM_R,
    "get_kickoff_fields": _FORM_R,
    "suggest_kickoff_fields": _FORM_R,
    "add_form_field_to_step": _FORM_W,
    "add_kickoff_field": _FORM_W,
    "delete_form_field": _FORM_W,
    "delete_kickoff_field": _FORM_W,
    "move_form_field": _FORM_W,
    "reorder_form_fields": _FORM_W,
    "reorder_kickoff_fields": _FORM_W,
    "update_dropdown_options": _FORM_W,
    "update_form_field": _FORM_W,
    "update_kickoff_field": _FORM_W,
    # -- automation ---------------------------------------------------------
    "analyze_template_automations": _AUTO_R,
    "get_step_visibility_conditions": _AUTO_R,
    "suggest_automation_consolidation": _AUTO_R,
    "create_automation_rule": _AUTO_W,
    "update_automation_rule": _AUTO_W,
    "delete_automation_rule": _AUTO_W,
    # -- processes read (folders over runs) ---------------------------------
    "get_process_folders": _PROC_R,
    # -- genuinely cross-family ---------------------------------------------
    # One response carries tasks, processes and templates.
    "search_all": frozenset(
        {MCPScopes.TASKS_READ, MCPScopes.PROCESSES_READ, MCPScopes.TEMPLATES_READ}
    ),
    # The universal escape hatch. Anything narrower here would make the rest of
    # this table decoration.
    "tallyfy_api_read": ALL_READ_SCOPES,
    "tallyfy_api_write": ALL_WRITE_SCOPES,
}

# Tools that require NO scope, each with the reason it needs none.
EXEMPT_TOOLS: Dict[str, str] = {
    "ask_user_question": (
        "Renders a prompt in the calling client. Makes no Tallyfy API call and "
        "reaches no Tallyfy resource."
    ),
    "ask_user_to_rank": (
        "Renders a drag-rank prompt in the calling client. Makes no Tallyfy API "
        "call and reaches no Tallyfy resource."
    ),
    "ask_user_to_confirm": (
        "Renders a confirmation prompt in the calling client. Makes no Tallyfy "
        "API call and reaches no Tallyfy resource."
    ),
    "validate_template_mapping": (
        "Pure, deterministic, no-AI/no-network validator over a mapping the "
        "caller already holds. Reads nothing from Tallyfy."
    ),
    "get_me": (
        "Identity bootstrap -- returns only the caller's own profile. api-v2's "
        "EnforceMcpTokenScopes leaves /me unmapped for exactly this reason, and "
        "every client needs its own user and organization id before it can "
        "address any other call, whatever scopes it holds."
    ),
    "get_organization": (
        "Organization bootstrap. Reads /organizations/{org}, which carries no "
        "resource segment, so api-v2's EnforceMcpTokenScopes returns null for "
        "it and leaves it ungated. Gating it here would break a client's first "
        "call without protecting a resource family."
    ),
}


@dataclass(frozen=True)
class ScopeDecision:
    """The outcome of one scope check, and why."""

    allowed: bool
    # One of: no_mcp_scopes_claim | exempt | granted | scope_missing | unmapped_tool
    reason: str
    required: FrozenSet[str] = frozenset()
    missing: FrozenSet[str] = frozenset()

    @property
    def gated(self) -> bool:
        """True when the caller presented an ``mcp_scopes`` claim to check."""
        return self.reason != "no_mcp_scopes_claim"


def scope_satisfied(required: str, granted: FrozenSet[str]) -> bool:
    """Is ``required`` covered by ``granted``?

    Mirrors ``EnforceMcpTokenScopes::scopeSatisfied``: an exact match, or the
    matching ``.write`` scope when a ``.read`` scope is required. Write implies
    read within a family; read NEVER implies write.
    """
    if required in granted:
        return True
    if required.endswith(READ_SUFFIX):
        write_scope = required[: -len(READ_SUFFIX)] + WRITE_SUFFIX
        return write_scope in granted
    return False


def required_scopes_for_tool(tool_name: str) -> Optional[FrozenSet[str]]:
    """The scopes ``tool_name`` requires, or ``None`` when it is exempt.

    Raises nothing for an unknown tool -- callers must distinguish "exempt" from
    "unknown", which ``decide`` does. Use that unless you specifically want the
    table lookup.
    """
    if tool_name in EXEMPT_TOOLS:
        return None
    return TOOL_SCOPES.get(tool_name)


def decide(tool_name: str, granted: Optional[Iterable[str]]) -> ScopeDecision:
    """Decide whether this caller may invoke ``tool_name``.

    Args:
        tool_name: The registered MCP tool name.
        granted: The token's ``mcp_scopes``, or ``None`` when the token carries
            no such claim. ``None`` and an empty list are DIFFERENT: ``None``
            means "not an MCP-issued token, do not gate" (see the module
            docstring -- this is the constraint that keeps chat.tallyfy.com and
            the desktop shell working), while ``[]`` means "an MCP token that
            was granted nothing", which is gated like any other.
    """
    if granted is None:
        return ScopeDecision(allowed=True, reason="no_mcp_scopes_claim")

    granted_set = frozenset(str(scope) for scope in granted)

    if tool_name in EXEMPT_TOOLS:
        return ScopeDecision(allowed=True, reason="exempt")

    required = TOOL_SCOPES.get(tool_name)
    if required is None:
        # FAIL CLOSED. A tool nobody mapped is a tool nobody decided was safe
        # for a narrowly-scoped token, and a new tool is exactly when that
        # decision matters most.
        return ScopeDecision(allowed=False, reason="unmapped_tool")

    missing = frozenset(s for s in required if not scope_satisfied(s, granted_set))
    if missing:
        return ScopeDecision(
            allowed=False,
            reason="scope_missing",
            required=required,
            missing=missing,
        )

    return ScopeDecision(allowed=True, reason="granted", required=required)
