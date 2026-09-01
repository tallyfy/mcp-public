"""
FastMCP Shared Parameter Types

This module provides reusable, validated parameter types for all MCP tools
following FastMCP best practices with Pydantic validation.
"""

from typing import Annotated, List, Optional, Dict, Any, Union
from pydantic import Field
from constants import TALLYFY_AUTH_SERVER, TOOL_SECURITY_METADATA, MCPScopes

# Core authentication types
TallyfyApiKey = Annotated[str, Field(
    min_length=32,
    description="Tallyfy API key (JWT token format)",
    examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."]
)]

OrganizationId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    pattern="^[a-f0-9]{32}$",
    description="Organization ID (32-character hexadecimal string)",
    examples=["a1b2c3d4e5f6789012345678901234ef"]
)]

# User-related types
# max_length mirrors api-v2: app/Http/Requests/User/InviteUserRequest.php:12 is
# 'bail|required|string|email:rfc,dns|max:255|banned_host'. Without it an
# over-length address costs a round trip and comes back as a 422, where a local
# Pydantic error names the constraint on turn one (#631).
UserEmail = Annotated[str, Field(
    max_length=255,
    pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    description="Valid email address (max 255 characters)",
    examples=["user@example.com"]
)]

UserId = Annotated[int, Field(
    gt=0,
    description="Unique user identifier",
    examples=[12345]
)]

OptionalUserId = Annotated[Optional[int], Field(
    gt=0,
    default=None,
    description="Optional numeric user identifier (positive integer when provided)",
    examples=[12345]
)]

OptionalGuestId = Annotated[Optional[str], Field(
      default=None,
      min_length=1,
      description="Unique guest identifier string extracted from the guest link URL",
      examples=["MITxZa1z2f5d81bb53f1da7c7fa95a2cfec5cbc2"]
  )]

# Bound comes from api-v2: app/Http/Requests/User/InviteUserRequest.php:16 is
# 'required|string|max:5000'. A dedicated type rather than OptionalString, per the
# "OptionalString is a last resort" rule in server/CLAUDE.md (#696) — an untyped
# optional publishes no format and no bound to the LLM, so an over-long invitation
# body only failed at the API as a 422 (#631).
#
# `required` on the api-v2 side is NOT mirrored here, and that is correct rather than
# an oversight: the SDK supplies a default before the request is built, so the tool
# genuinely may omit it.
InvitationMessage = Annotated[Optional[str], Field(
    default=None,
    max_length=5000,
    description="Custom invitation message (optional, max 5000 characters)",
    examples=["Welcome to the team. This is where we track our onboarding."]
)]

UserRole = Annotated[str, Field(
    pattern="^(light|standard|admin)$",
    description="User role in the organization (light, standard, admin)",
    examples=["light"]
)]

# api-v2 caps MEMBER names at 32 (InviteUserRequest.php:13-14,
# UpdateUserRequest.php:12-13 — 'required|string|max:32|disallowed_name|is_not_url').
UserName = Annotated[str, Field(
    min_length=1,
    max_length=32,
    description="Organization member's first or last name (max 32 characters)",
    examples=["John", "Smith"]
)]

# Guests are a DIFFERENT contract: api-v2 allows 200
# (CreateGuestRequest.php:13-14 — 'nullable|max:200|string').
GuestName = Annotated[str, Field(
    min_length=1,
    max_length=200,
    description="Guest's first or last name (max 200 characters)",
    examples=["John", "Smith"]
)]

# Names are OPTIONAL on a guest (#622 item 8). api-v2's CreateGuestRequest declares
# 'first_name' => 'nullable|max:200|string' and requires only 'email', so a mandatory
# GuestName made an email-only guest legal at the API and impossible through the tool.
# No min_length: a blank name is treated as "not supplied" and its key is omitted.
OptionalGuestName = Annotated[Optional[str], Field(
    default=None,
    max_length=200,
    description="Guest's first or last name (optional, max 200 characters)",
    examples=["John", "Smith"]
)]

# Task-related types
#
# NOTE (#696): TaskId and ProcessId are structurally identical (32-char hex), so the
# ONLY thing that tells an LLM them apart in the JSON Schema is the description and the
# example. They shared the example value `a1b2c3d4e5f6789012345678901234ef` until #696,
# which is one of the two reasons `get_task_comments` was being called with the task id
# in the run_id slot. Keep these two example values DISTINCT.
TaskId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    description="Unique task identifier",
    pattern="^[a-f0-9]{32}$",
    examples=["f4a1c2b39e7d48a6b0c15e2d3f6a8b90"]
)]

# api-v2: CreateOneOffTaskRequest.php:19 / UpdateTasksRequest.php:61 -> 'max:600'
TaskTitle = Annotated[str, Field(
    min_length=1,
    max_length=600,
    description="Task title or name (max 600 characters)",
    examples=["Review quarterly report"]
)]

# TaskDescription was REMOVED in #631. It had zero usages and carried
# max_length=2000, the same guessed cap that StepDescription carried and that api-v2
# was measured not to impose. Rather than copy an unverified bound onto a second
# type, it is gone: a future task-description parameter should derive its bound from
# the api-v2 FormRequest that governs it, or carry none.

NaturalLanguageInput = Annotated[str, Field(
    min_length=3,
    max_length=2000,
    description="Natural language text input for processing",
    examples=["Create a task called Review Document due next Monday at 2PM"]
)]

# Process-related types
# The example here MUST differ from TaskId's. See the note above TaskId (#696).
ProcessId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    description="Unique process (run) identifier",
    pattern="^[a-f0-9]{32}$",
    examples=["c7d8e9f0a1b2c3d4e5f60718293a4b5c"]
)]

# Optional counterpart to ProcessId (#696).
#
# Use this, never OptionalString, for an optional run/process id parameter. OptionalString
# publishes `{"type": ["string","null"], "description": "Optional string parameter"}`, which
# carries no format, no length and no semantics, so a model holding a task id will happily
# put it in that slot. This annotation publishes the same 32-hex pattern as ProcessId plus a
# description that says outright it is not the task id.
#
# The EMPTY STRING must stay valid, and that is not cosmetic. On develop this
# parameter was OptionalString, so `run_id=""` was falsy, the #189 resolver ran
# and the call succeeded. Adding `min_length=32` made Pydantic reject it before
# the function body, and FastMCP validates the signature BEFORE the body runs,
# so no in-body guard can rescue it. An LLM that emits "" rather than omitting
# an optional field is common, so that would have broken a working path in the
# name of tightening it. The pattern therefore permits empty or 32-hex, and
# nothing in between; `max_length` alone bounds the length.
OptionalProcessId = Annotated[Optional[str], Field(
    default=None,
    max_length=32,
    pattern="^(?:[a-f0-9]{32})?$",
    description=(
        "Process (run) ID that the task belongs to, as a 32-character hex string. "
        "This identifies the PROCESS, not the task: never pass the same value here "
        "as task_id. Omit it entirely if you do not have it and it will be looked up."
    ),
    examples=["c7d8e9f0a1b2c3d4e5f60718293a4b5c"]
)]

# api-v2: CreateRunRequest.php:38 / UpdateRunRequest.php:26 -> 'string|max:550'
ProcessTitle = Annotated[str, Field(
    min_length=1,
    max_length=550,
    description="Process title or name (max 550 characters)",
    examples=["Employee Onboarding"]
)]

# Vocabulary is api-v2's, read from app/Models/Run.php:970:
#   ['active', 'complete', 'problem', 'improvement', 'archived', 'delayed', 'starred']
# Until #631 this read "^(active|completed|cancelled|paused)$" — only `active`
# overlapped. `completed`, `cancelled` and `paused` do not exist, and the other six
# real statuses were missing. It had ZERO usages, which is exactly why it was worth
# correcting rather than leaving: the next person writing a run-status tool reaches
# for the type whose name is already right, and inherits a filter that rejects every
# legal value. A wrong-but-unused shared type is a landmine, not dead code.
ProcessStatus = Annotated[str, Field(
    pattern="^(active|complete|problem|improvement|archived|delayed|starred)$",
    description=(
        "Process (run) status: active, complete, problem, improvement, archived, "
        "delayed, starred"
    ),
    examples=["active"]
)]

# Template-related types
TemplateId = Annotated[str, Field(
    description="Unique template identifier",
    pattern="^[a-f0-9]{32}$",
    examples=["a1b2c3d4e5f6789012345678901234ef"],
    min_length=32,
    max_length=32
)]

# api-v2: CreateChecklistRequest.php:15 / UpdateChecklistRequest.php:16 -> 'required|max:250'
TemplateTitle = Annotated[str, Field(
    min_length=1,
    max_length=250,
    description="Template title or name (max 250 characters)",
    examples=["New Employee Onboarding Template"]
)]

# TemplateDescription was REMOVED in #631, for the same reason as TaskDescription
# directly above: zero usages, and an unverified max_length=2000.

# Step-related types
StepId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    description="Unique step identifier",
    pattern="^[a-f0-9]{32}$",
    examples=["a1b2c3d4e5f6789012345678901234ef"]
)]

# api-v2: CreateStepRequest.php:17 / UpdateStepRequest.php:92 -> 'required|max:600'
StepTitle = Annotated[str, Field(
    min_length=1,
    max_length=600,
    description="Step title or name (max 600 characters)",
    examples=["Review document", "Complete form"]
)]

# NO max_length, deliberately. api-v2 imposes none: both
# app/Http/Requests/Steps/CreateStepRequest.php and UpdateStepRequest.php declare
# 'summary' => 'nullable', over a `text` column. The 2000-character cap this
# carried until #631 was a guessed value, and it REJECTED input the API would have
# stored — a user pasting a 3,000-character SOP excerpt got a Pydantic error naming
# a limit that does not exist, so the model either truncated their content silently
# or reported a false constraint. An over-strict type fails in the direction nobody
# sees, because no server-side error is ever produced. Do not reintroduce a cap here
# without a real api-v2 rule to cite.
StepDescription = Annotated[str, Field(
    description="Step description or summary",
    examples=["Please review the attached document and provide feedback"]
)]

# Form field types
FieldId = Annotated[str, Field(
    description="Unique form field identifier",
    pattern="^[a-f0-9]{32}$",
    examples=["a1b2c3d4e5f6789012345678901234ef"],
    min_length=32,
    max_length=32

)]

# FieldName was REMOVED in #631. Zero usages, an unverified max_length=100, and a
# name that does not correspond to anything api-v2 stores: a capture carries `label`
# (see FieldLabel above) and an `alias`, not a "name". Its snake_case examples
# suggested an alias while its description said label, so adopting it would have
# meant picking one of two contracts at random.

# Mirrors what tools/form_fields.py actually enforces. api-v2's canonical set is
# BaseCapture::$field_types (BaseCapture.php:183-194) = the 9 below PLUS "email";
# "email" is deliberately excluded here because the native Tallyfy UI cannot render
# it (issue #439). There is NO `number` and NO `checkbox` field type in either set.
FieldType = Annotated[str, Field(
    pattern="^(text|textarea|radio|dropdown|multiselect|date|file|table|assignees_form)$",
    description="Form field type",
    examples=["text"]
)]

# NO max_length. api-v2's CaptureRequestValidator.php:22 is `$prefix.'label' =>
# 'required'` with no length rule, over a `label text` column. The 200-character cap
# this carried until #631 was invented. `min_length=1` is kept because `required`
# genuinely rejects an empty label.
FieldLabel = Annotated[str, Field(
    min_length=1,
    description="Form field display label",
    examples=["Employee Name", "Start Date"]
)]

# Form-field positions are 1-BASED and capped, exactly like StepPosition below.
# api-v2's MoveCaptureRequest validates 'required|integer|between:1,9999'
# (app/Http/Requests/Captures/MoveCaptureRequest.php:13, message at :20), and the
# sibling app/Http/Requests/Checklists/CapturesReorderRequest.php:19 uses
# 'digits_between:1,4|gte:1' — 1..9999 from both ends. This
# previously declared ge=0 with no upper bound, so the schema invited position=0
# and unbounded values that the API then rejected — the same drift as #581.
FieldPosition = Annotated[int, Field(
    ge=1,
    le=9999,
    description="Position/order of the field in the form (1-BASED: the first field is position 1, not 0)",
    examples=[1, 2, 3]
)]

# Search-related types
SearchQuery = Annotated[str, Field(
    min_length=1,
    max_length=500,
    description="Search query string",
    examples=["quarterly report", "employee onboarding"]
)]

# Vocabulary is api-v2's, read from SearchController.php's repo_map (:35-42) and the
# documented enum at :83: blueprint, process, task, snippet, capture, step.
#
# Until #631 this read "^(all|tasks|processes|templates|users)$" — NOT ONE of those
# five values exists, so any tool adopting the type would have rejected every legal
# input. The trap is that `tools/search.py::VALID_SEARCH_TYPES` has always held the
# correct six, so the module reads as verified while this shared type, sitting under
# a more obvious name, was never reconciled against anything.
#
# Kept in sync with that constant deliberately; if the two ever disagree, search.py
# is the one with live callers and this is the one to fix.
SearchType = Annotated[str, Field(
    pattern="^(blueprint|process|task|snippet|capture|step)$",
    description=(
        "Type of items to search: blueprint, process, task, snippet, capture, step"
    ),
    examples=["blueprint"]
)]

# Tag-related types
#
# api-v2's rule is `'color' => 'nullable|string|size:7'`
# (app/Http/Requests/Tags/CreateTagRequest.php). `size:7` on a string is EXACTLY 7
# characters, not a maximum, so `#FF5733` is the only shape that fits: 8-char values
# carrying an alpha channel (`#FF5733AA`), 4-char shorthand (`#F53`) and named colours
# (`red`) are all rejected by the API. Those three are exactly what a model produces
# when nothing constrains it, so the bound is enforced here to turn a 422 into a local
# error that names the required shape (#620).
#
# The pattern is narrower than api-v2's rule on purpose. api-v2 checks length only, so
# `1234567` would satisfy it and store a value no client can render. Requiring the hex
# shape rejects that too, and no legitimate colour is lost: the only 7-character value
# Tallyfy's own UI ever writes is `#RRGGBB`.
TagColor = Annotated[Optional[str], Field(
    default=None,
    pattern=r"^#[0-9A-Fa-f]{6}$",
    description="Hex color, exactly 7 characters as #RRGGBB (e.g. #FF5733). "
                "Shorthand (#F53), alpha (#FF5733AA) and named colors are rejected.",
    examples=["#FF5733", "#01803D"]
)]

# Automation-related types
RuleId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    description="Unique automation rule identifier",
    pattern="^[a-f0-9]{32}$",
    examples=["a1b2c3d4e5f6789012345678901234ef"]
)]

# api-v2: AutomatedActionRequest.php:15 -> 'automated_alias' => 'required|string|max:300'
RuleName = Annotated[str, Field(
    min_length=1,
    max_length=300,
    description="Automation rule name (max 300 characters)",
    examples=["Auto-assign manager tasks"]
)]

# RuleCondition and RuleAction were REMOVED in #631, and these two were the most
# dangerous of the twelve rather than merely unused.
#
# Both typed an automation rule as a free-text STRING, with examples describing a
# query DSL ("task.title contains 'urgent'", "assign to manager") that Tallyfy does
# not have. Real automations are STRUCTURED objects: `tools/automation.py` builds
# conditions and actions as dicts and validates them against api-v2's
# AutomatedActionRequest vocabularies. A future author reaching for the
# correctly-named shared type would have been steered into inventing a rule syntax,
# which no amount of adjusting a max_length would fix.
#
# If typed automation parameters are ever wanted, model them on the constants in
# `tools/template_mapping_validation.py`, which are reconciled against api-v2.

AutomationId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    description="Unique automation identifier",
    pattern="^[a-f0-9]{32}$",
    examples=["a1b2c3d4e5f6789012345678901234ef"]
)]

# Common optional types
OptionalString = Annotated[Optional[str], Field(
    description="Optional string parameter"
)]

OptionalInt = Annotated[Optional[int], Field(
    ge=0,
    description="Optional integer parameter (non-negative)"
)]

OptionalBool = Annotated[Optional[bool], Field(
    description="Optional boolean parameter"
)]

# `top_secret` was an OptionalBool, which publishes the schema entry "Optional
# boolean parameter" and says nothing at all. #585 asked for two things: that the
# written value be readable back, and that the flag have a documented purpose.
# The read half is done (`_task_after_write` echoes it, and TaskTransformer emits
# it on every task read); this is the half a read path cannot fix, because a model
# handed a bare boolean has no way to know what setting it does.
#
# The behaviour is read from api-v2 `app/Scopes/SecretTaskScope.php`, not from a
# doc page: the scope returns early for an org admin, and for everyone else
# restricts the query to `top_secret = false` OR the caller being one of the
# task's owners.
#
# It lives here rather than in either tool description for two reasons. A
# parameter description is published in the JSON schema and does NOT count against
# the 2000-BYTE tool-description cap, and `update_standalone_task` sits at 1917
# bytes with 83 to spare while `update_task` sits at 1964 with 36. And
# `update_task` takes the identical flag, so one shared type is the only shape
# these two cannot drift apart in (rule 16).
TopSecretFlag = Annotated[Optional[bool], Field(
    description=(
        "Restrict who can see this task. true hides it from every member of the "
        "organization EXCEPT its assignees and organization admins; false makes it "
        "visible to the organization as usual. Omit to leave the current setting "
        "alone. Read the stored value back with get_task or get_standalone_task, "
        "which return top_secret on every task."
    )
)]

# List types for bulk operations
UserIdList = Annotated[Optional[List[int]], Field(
    description="List of user IDs for bulk operations",
    examples=[[12345, 67890]]
)]

# UserEmailList, UserNameList, TagList and GroupIdList are also unused today, and
# #631 deliberately KEPT them where it removed five siblings. The distinction is not
# usage, it is whether the type asserts something that could be false: these four are
# plain Optional[List[str]] with a description and an example, and carry no bound,
# pattern or vocabulary. There is nothing in them to be wrong. The five that went
# each pinned a number or a shape api-v2 does not impose, which is what makes an
# unused type a landmine rather than dead weight.
UserEmailList = Annotated[Optional[List[str]], Field(
    description="List of email addresses",
    examples=[["user1@example.com", "user2@example.com"]]
)]

UserNameList = Annotated[Optional[List[str]], Field(
    description="List of user names",
    examples=[["John Smith", "Jane Doe"]]
)]

TagList = Annotated[Optional[List[str]], Field(
    description="List of tags for categorization",
    examples=[["urgent", "quarterly", "finance"]]
)]

# Pagination types
PageNumber = Annotated[Optional[int], Field(
    ge=1,
    description="Page number for pagination (starting from 1)",
    examples=[1]
)]

PageSize = Annotated[Optional[int], Field(
    ge=1,
    le=100,
    description="Number of items per page (1-100)",
    examples=[20]
)]

# Date/time types
DateString = Annotated[Optional[str], Field(
    description="Date in ISO format or natural language",
    examples=["2025-05-01", "next Monday", "tomorrow at 2PM"]
)]

TimestampString = Annotated[Optional[str], Field(
    description="Timestamp in ISO format",
    examples=["2025-05-01T14:30:00Z"]
)]

# Group-related types (for future use)
# core.groups.id is character varying(32) (db-schema.sql:3472) — a 32-char hex
# string, NOT a "group_<n>" slug. Verified live against GET /organizations/{org}/groups.
GroupId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    pattern="^[a-f0-9]{32}$",
    description="Group ID (32-character hexadecimal string)",
    examples=["53a4560dc877f9b4f709f97630efbb5a"]
)]

# api-v2: CreateGroupRequest.php:13 / UpdateGroupRequest.php:15 -> 'required|max:200|string'
GroupName = Annotated[str, Field(
    min_length=1,
    max_length=200,
    description="Group name (max 200 characters)",
    examples=["Development Team", "Finance Department"]
)]

GroupIdList = Annotated[Optional[List[str]], Field(
    description="List of group IDs (32-character hexadecimal strings)",
    examples=[["53a4560dc877f9b4f709f97630efbb5a", "149fca2c855069c8a7d0280fd19d9cf7"]]
)]

# Tag-related types
# core.tags.id is character varying(32) (db-schema.sql:5016) — a 32-char hex string,
# NOT a "tag_<slug>". Verified live against GET /organizations/{org}/tags.
TagId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    pattern="^[a-f0-9]{32}$",
    description="Tag ID (32-character hexadecimal string)",
    examples=["a1424ae2d796fbaba9e6b46cb0453475"]
)]

# api-v2: CreateTagRequest.php:12 -> 'required|unique:...|max:30'
TagTitle = Annotated[str, Field(
    min_length=1,
    max_length=30,
    description="Tag title (max 30 characters)",
    examples=["urgent", "quarterly"]
)]

# Folder-related types
FolderId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    pattern="^[a-f0-9]{32}$",
    description="Folder ID (32-character hexadecimal string)",
    examples=["7c9e6679742540de944be07fc1f90ae7"]
)]

# api-v2: CreateFolderRequest.php:16 / UpdateFolderRequest.php:21 -> 'required|max:32'
FolderName = Annotated[str, Field(
    min_length=1,
    max_length=32,
    description="Folder name (max 32 characters)",
    examples=["Q1 2026", "Finance"]
)]

# core.folder_objects.id is an auto-increment INTEGER (db-schema.sql:3418),
# not a hex string — the value comes back on the folder-object relation
# created by add_object_to_folder.
FolderObjectId = Annotated[int, Field(
    ge=1,
    description="Folder-object relation ID (positive integer)",
    examples=[12345]
)]

FolderType = Annotated[str, Field(
    description="Folder kind: 'checklist' for template folders, 'run' for process folders",
    examples=["checklist", "run"]
)]

# Comment-related types
# core.threads.id is character varying(32) (db-schema.sql:5346) — a 32-char hex
# string, NOT a "comment_<slug>".
CommentId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    pattern="^[a-f0-9]{32}$",
    description="Comment (thread) ID (32-character hexadecimal string)",
    examples=["a1b2c3d4e5f6789012345678901234ef"]
)]

# Step position type
# Step positions are 1-BASED. api-v2's own StepService::reorderStep docblock says
# "(1-based)", and live templates confirm it (minimum observed position is 1, never 0).
# This previously declared ge=0 with a "0-based index" description and examples
# [0, 1, 2], so callers followed the schema, sent 0, and were rejected (#581).
StepPosition = Annotated[int, Field(
    ge=1,
    description="Step position (1-BASED: the first step is position 1, not 0)",
    examples=[1, 2, 3]
)]

# Generic container types for flexible responses
GenericDict = Annotated[Dict[str, Any], Field(
    description="Generic dictionary response"
)]

GenericList = Annotated[List[Dict[str, Any]], Field(
    description="Generic list of dictionary responses"
)]

FieldIdList = Annotated[List[str], Field(
    description="Ordered list of field IDs (32-char hex strings)",
    min_length=1,
    examples=[["a1b2c3d4e5f6789012345678901234ef", "b2c3d4e5f67890123456789012345678"]]
)]

# Reorder input that accepts BOTH a bare field-ID string and an explicit
# {capture_id (or id), position} object. reorder_form_fields normalizes both in its
# body, but FastMCP validates the signature with Pydantic BEFORE the body runs, so a
# bare List[Dict] type rejects the documented "list of field IDs" call at the schema
# layer and the string-handling branch is dead code (#628). The union widens the type
# so both shapes pass validation; the dict shape preserves explicit, non-sequential
# positions that a bare List[str] cannot express.
FieldOrderList = Annotated[List[Union[str, Dict[str, Any]]], Field(
    min_length=1,
    description=(
        "Ordered list of field IDs (32-char hex strings), OR a list of "
        "{capture_id, position} objects for explicit positions"
    ),
    examples=[
        ["a1b2c3d4e5f6789012345678901234ef", "b2c3d4e5f67890123456789012345678"],
        [{"capture_id": "a1b2c3d4e5f6789012345678901234ef", "position": 1}],
    ]
)]




def get_tool_security_meta(
    scopes: List[str] = None,
    read_only: bool = True,
    category: str = "tasks"
) -> Dict[str, Any]:
    """
    Get security metadata for a tool declaration.

    Args:
        scopes: Required OAuth scopes (overrides category-based defaults)
        read_only: Whether tool only reads data (affects default scopes)
        category: Tool category for default scopes: users, tasks, processes,
                  templates, forms, automation

    Returns:
        Dictionary of security metadata for tool's `meta` parameter

    Usage:
        @mcp.tool(
            name="get_organization_users",
            meta=get_tool_security_meta(category="users", read_only=True)
        )
    """
    if scopes is None:
        scope_map = {
            "users": (MCPScopes.USERS_READ, MCPScopes.USERS_WRITE),
            "tasks": (MCPScopes.TASKS_READ, MCPScopes.TASKS_WRITE),
            "processes": (MCPScopes.PROCESSES_READ, MCPScopes.PROCESSES_WRITE),
            "templates": (MCPScopes.TEMPLATES_READ, MCPScopes.TEMPLATES_WRITE),
            "forms": (MCPScopes.FORMS_READ, MCPScopes.FORMS_WRITE),
            "automation": (MCPScopes.AUTOMATION_READ, MCPScopes.AUTOMATION_WRITE),
        }
        read_scope, write_scope = scope_map.get(category, (MCPScopes.TASKS_READ, MCPScopes.TASKS_WRITE))
        scopes = [read_scope] if read_only else [read_scope, write_scope]

    return {
        "security": [{"oauth2": scopes}],
        "authentication": "required",
    }