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

UserName = Annotated[str, Field(
    min_length=1,
    max_length=100,
    description="User's first or last name",
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

TaskTitle = Annotated[str, Field(
    min_length=1,
    max_length=255,
    description="Task title or name",
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

ProcessTitle = Annotated[str, Field(
    min_length=1,
    max_length=255,
    description="Process title or name",
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

TemplateTitle = Annotated[str, Field(
    min_length=1,
    max_length=255,
    description="Template title or name",
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

StepTitle = Annotated[str, Field(
    min_length=1,
    max_length=255,
    description="Step title or name",
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

FieldType = Annotated[str, Field(
    pattern="^(text|number|date|dropdown|checkbox|textarea|file)$",
    description="Form field type",
    examples=["text"]
)]

FieldLabel = Annotated[str, Field(
    min_length=1,
    max_length=200,
    description="Form field display label",
    examples=["Employee Name", "Start Date"]
)]

FieldPosition = Annotated[int, Field(
    ge=0,
    description="Position/order of the field in the form",
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

RuleName = Annotated[str, Field(
    min_length=1,
    max_length=200,
    description="Automation rule name",
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
GroupId = Annotated[str, Field(
    min_length=1,
    description="Unique group identifier",
    examples=["group_303"]
)]

GroupName = Annotated[str, Field(
    min_length=1,
    max_length=100,
    description="Group name",
    examples=["Development Team", "Finance Department"]
)]

GroupIdList = Annotated[Optional[List[str]], Field(
    description="List of group IDs",
    examples=[["group_303", "group_404"]]
)]

# Tag-related types
TagId = Annotated[str, Field(
    min_length=1,
    description="Tag ID",
    examples=["tag_abc123"]
)]

TagTitle = Annotated[str, Field(
    min_length=1,
    description="Tag title",
    examples=["urgent", "quarterly"]
)]

# Folder-related types
FolderId = Annotated[str, Field(
    min_length=1,
    description="Folder ID",
    examples=["folder_abc123"]
)]

FolderName = Annotated[str, Field(
    min_length=1,
    description="Folder name",
    examples=["Q1 2026", "Finance"]
)]

FolderObjectId = Annotated[str, Field(
    min_length=1,
    description="Folder-object relation ID",
    examples=["fo_abc123"]
)]

# Comment-related types
CommentId = Annotated[str, Field(
    min_length=1,
    description="Comment ID",
    examples=["comment_abc123"]
)]

# Step position type
StepPosition = Annotated[int, Field(
    ge=0,
    description="Step position (0-based index)",
    examples=[0, 1, 2]
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