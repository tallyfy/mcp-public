"""
FastMCP Shared Parameter Types

This module provides reusable, validated parameter types for all MCP tools
following FastMCP best practices with Pydantic validation.
"""

from typing import Annotated, List, Optional, Dict, Any
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
UserEmail = Annotated[str, Field(
    pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    description="Valid email address",
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

# Task-related types
TaskId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    description="Unique task identifier",
    pattern="^[a-f0-9]{32}$",
    examples=["a1b2c3d4e5f6789012345678901234ef"]
)]

# api-v2: CreateOneOffTaskRequest.php:19 / UpdateTasksRequest.php:61 -> 'max:600'
TaskTitle = Annotated[str, Field(
    min_length=1,
    max_length=600,
    description="Task title or name (max 600 characters)",
    examples=["Review quarterly report"]
)]

TaskDescription = Annotated[str, Field(
    max_length=2000,
    description="Detailed task description",
    examples=["Please review the Q4 financial report and provide feedback"]
)]

NaturalLanguageInput = Annotated[str, Field(
    min_length=3,
    max_length=2000,
    description="Natural language text input for processing",
    examples=["Create a task called Review Document due next Monday at 2PM"]
)]

# Process-related types
ProcessId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    description="Unique process (run) identifier",
    pattern="^[a-f0-9]{32}$",
    examples=["a1b2c3d4e5f6789012345678901234ef"]
)]

# api-v2: CreateRunRequest.php:38 / UpdateRunRequest.php:26 -> 'string|max:550'
ProcessTitle = Annotated[str, Field(
    min_length=1,
    max_length=550,
    description="Process title or name (max 550 characters)",
    examples=["Employee Onboarding"]
)]

ProcessStatus = Annotated[str, Field(
    pattern="^(active|completed|cancelled|paused)$",
    description="Process status (active, completed, cancelled, paused)",
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

TemplateDescription = Annotated[str, Field(
    max_length=2000,
    description="Template description",
    examples=["Standard onboarding process for new employees"]
)]

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

StepDescription = Annotated[str, Field(
    max_length=2000,
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

FieldName = Annotated[str, Field(
    min_length=1,
    max_length=100,
    description="Form field name",
    examples=["employee_name", "start_date"]
)]

# Mirrors what tools/form_fields.py actually enforces. api-v2's canonical set is
# BaseCapture::$field_types (BaseCapture.php:183-194) = the 9 below PLUS "email";
# "email" is deliberately excluded here because the native Tallyfy UI cannot render
# it (issue #439). There is NO `number` and NO `checkbox` field type in either set.
FieldType = Annotated[str, Field(
    pattern="^(text|textarea|radio|dropdown|multiselect|date|file|table|assignees_form)$",
    description="Form field type",
    examples=["text"]
)]

FieldLabel = Annotated[str, Field(
    min_length=1,
    max_length=200,
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

SearchType = Annotated[str, Field(
    pattern="^(all|tasks|processes|templates|users)$",
    description="Type of items to search (all, tasks, processes, templates, users)",
    examples=["all"]
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

RuleCondition = Annotated[str, Field(
    min_length=1,
    max_length=500,
    description="Rule trigger condition",
    examples=["task.title contains 'urgent'"]
)]

RuleAction = Annotated[str, Field(
    min_length=1,
    max_length=500,
    description="Rule action to execute",
    examples=["assign to manager"]
)]

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

# List types for bulk operations
UserIdList = Annotated[Optional[List[int]], Field(
    description="List of user IDs for bulk operations",
    examples=[[12345, 67890]]
)]

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