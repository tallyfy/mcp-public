"""
MCP Server Capabilities Declaration

Explicitly declares server capabilities for standards compliance with:
- Anthropic Claude Connectors Directory submission
- OpenAI ChatGPT App submission
- MCP specification compliance
"""

from unittest.mock import MagicMock

from mcp.types import ServerCapabilities, ToolsCapability, ResourcesCapability

from constants import MCP_RESOURCE_URL
# Imported, not re-listed: this is the exact array the live
# /.well-known/oauth-authorization-server document publishes, and clients
# believe that document. A hand-copied list here previously advertised the
# internal Python constant NAMES (USERS_READ, ...) rather than the wire
# values (mcp.users.read, ...), and was missing four scopes.
from routes.oauth import SUPPORTED_SCOPES

# (label, tools module, description). The per-category TOOL COUNT is NOT
# written here — it is derived from the module's own registrations by
# ``category_breakdown()`` below.
#
# It used to be a hand-maintained integer per row and it drifted badly: the
# list advertised 110 tools across 15 categories while the server had 108,
# including two categories (Template Generation, Text AI Helpers) whose tools
# were deleted in PR #492 and omitting Template Mapping Validation entirely.
# This file's whole job is to describe the server accurately to directory
# reviewers, so the numbers now come from the same source of truth the server
# registers from. See #654.
CATEGORY_DESCRIPTIONS = [
    ("User Management", "user_management", "Organization members, guests, invitations, user/guest CRUD, role management"),
    ("Task Management", "task_management", "Task CRUD, standalone tasks, NLP date extraction, kickoff forms"),
    ("Process Management", "process_management", "Workflow process operations, reactivation, kickoff forms"),
    ("Template Management", "template_management", "Blueprint/template CRUD, step management, cloning"),
    ("Form Fields", "form_fields", "Dynamic form field management, suggestions, reordering"),
    ("Search", "search", "Task, process, template, snippet search, plus cross-type search_all"),
    ("Automation", "automation", "If-then rules, creation, analysis, redundancy detection, consolidation suggestions"),
    ("Group Management", "group_management", "Team/group CRUD and membership"),
    ("Comment Management", "comment_management", "Task comments, issue reporting and resolution"),
    ("Tag Management", "tag_management", "Tag CRUD, template/process tagging"),
    ("Folder Management", "folder_management", "Folder CRUD, object-folder organization"),
    ("User Interaction", "user_interaction", "Structured user questions, ranking, and confirmation"),
    ("Template Mapping Validation", "template_mapping_validation", "Deterministic validation of a drafted template mapping before it is built"),
    ("Universal API Fallback", "api_fallback", "Catch-all read and write access to any Tallyfy REST API endpoint"),
]

_breakdown_cache: list | None = None


def _count_tools_in_module(module_name: str) -> int:
    """Register a tools module against a throwaway mcp and count the result.

    Registration is pure decoration, so running it a second time against a
    stub has no effect on the real server.
    """
    import importlib

    module = importlib.import_module(f"tools.{module_name}")
    names = set()

    def stub_tool(**kwargs):
        def decorator(func):
            names.add(kwargs.get("name", func.__name__))
            return func
        return decorator

    stub = MagicMock()
    stub.tool = stub_tool
    for attr in dir(module):
        if attr.startswith("register_") and callable(getattr(module, attr)):
            getattr(module, attr)(stub)
    return len(names)


def category_breakdown() -> list:
    """Return [(label, tool_count, description)], counted from the code.

    Memoized — the tool set is static for the life of the process
    (``ToolsCapability(listChanged=False)``).
    """
    global _breakdown_cache
    if _breakdown_cache is None:
        _breakdown_cache = [
            (label, _count_tools_in_module(module), desc)
            for label, module, desc in CATEGORY_DESCRIPTIONS
        ]
    return _breakdown_cache


def register_capabilities(mcp):
    """Register explicit server capabilities endpoint."""

    @mcp.resource("tallyfy://capabilities")
    async def get_server_capabilities() -> str:
        """Get explicit declaration of server capabilities for standards compliance"""
        capabilities = ServerCapabilities(
            tools=ToolsCapability(listChanged=False),
            resources=ResourcesCapability(listChanged=False),
            prompts=None,
            logging=None,
            completions=None,
            tasks=None,
        )

        tool_count = len(await mcp.list_tools())
        breakdown = category_breakdown()
        num_categories = len(breakdown)
        scopes_text = ", ".join(SUPPORTED_SCOPES)

        cat_lines = []
        for label, count, desc in breakdown:
            noun = "tool" if count == 1 else "tools"
            cat_lines.append(f"- **{label}** ({count} {noun}): {desc}")
        categories_text = "\n".join(cat_lines)

        return f"""# Tallyfy MCP Server Capabilities

## Supported Capabilities

{format_capabilities(capabilities)}

## Tools ({tool_count} tools across {num_categories} categories)

{categories_text}

## Authentication

- **OAuth 2.1**: Authorization Code Flow + PKCE, with Dynamic Client Registration
- **JWT Validation**: RS256 signature verification
- **Issuer**: {MCP_RESOURCE_URL}
- **Discovery**: `{MCP_RESOURCE_URL}/.well-known/oauth-authorization-server` and
  `{MCP_RESOURCE_URL}/.well-known/oauth-protected-resource` are authoritative
- **Scopes**: {scopes_text}

## Transport

- **Protocol**: MCP (Model Context Protocol) v1.0
- **Transport**: Streamable HTTP, served at `/`, `/mcp` and `/mcp/`
- **Format**: JSON (application/json)

## Response Constraints

- **Max Size**: 25KB per tool result (auto-compacted)
- **Streaming**: Supported via HTTP chunked transfer encoding
- **Error Handling**: Graceful with ToolError for validation, TallyfyError for API errors

## Standards Compliance

- MCP Protocol v1.0 compliant
- OAuth 2.1 with PKCE (RFC 7636)
- Tool safety annotations (readOnlyHint, destructiveHint, idempotentHint)
- Response minimization (no diagnostic fields)
- Sensitive data protection (JWT tokens never logged)
- HTTPS with valid TLS certificate

## Not Supported

- Prompts: Not implemented
- Logging: Not implemented
- Completions: Not implemented
- Tasks: Not implemented
- Sampling: Not implemented
"""


def format_capabilities(capabilities: ServerCapabilities) -> str:
    """Format ServerCapabilities as readable text."""
    lines = []

    if capabilities.tools:
        lines.append("### Tools: SUPPORTED")
        if capabilities.tools.listChanged is False:
            lines.append("  - List changes: No (static tool set)")
        else:
            lines.append("  - List changes: Yes (tools may be added/removed)")

    if capabilities.resources:
        lines.append("### Resources: SUPPORTED")
        if capabilities.resources.listChanged is False:
            lines.append("  - List changes: No (static resource set)")
        else:
            lines.append("  - List changes: Yes (resources may be added/removed)")

    if not capabilities.prompts:
        lines.append("### Prompts: NOT SUPPORTED")

    if not capabilities.logging:
        lines.append("### Logging: NOT SUPPORTED")

    if not capabilities.completions:
        lines.append("### Completions: NOT SUPPORTED")

    if not capabilities.tasks:
        lines.append("### Tasks: NOT SUPPORTED")

    return "\n".join(lines)
