"""
MCP Server Capabilities Declaration

Explicitly declares server capabilities for standards compliance with:
- Anthropic Claude Connectors Directory submission
- OpenAI ChatGPT App submission
- MCP specification compliance
"""

from mcp.types import ServerCapabilities, ToolsCapability, ResourcesCapability

CATEGORY_DESCRIPTIONS = [
    ("User Management", 14, "Organization members, guests, invitations, user/guest CRUD, role management"),
    ("Task Management", 13, "Task CRUD, standalone tasks, NLP date extraction, kickoff forms"),
    ("Process Management", 7, "Workflow process operations, reactivation, kickoff forms"),
    ("Template Management", 18, "Blueprint/template CRUD, step management, cloning"),
    ("Form Fields", 8, "Dynamic form field management, suggestions, reordering"),
    ("Search", 5, "Task, process, template, snippet search, plus cross-type search_all"),
    ("Automation", 6, "If-then rules, creation, analysis, redundancy detection, consolidation suggestions"),
    ("Group Management", 7, "Team/group CRUD and membership"),
    ("Comment Management", 6, "Task comments, issue reporting and resolution"),
    ("Tag Management", 8, "Tag CRUD, template/process tagging"),
    ("Folder Management", 7, "Folder CRUD, object-folder organization"),
    ("User Interaction", 3, "Structured user questions, ranking, and confirmation"),
    ("Template Generation", 5, "AI-driven template draft creation from prompts, documents, and images"),
    ("Text AI Helpers", 2, "AI-powered name and procedure suggestions"),
    ("Universal API Fallback", 1, "Catch-all for any Tallyfy REST API endpoint"),
]


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
        num_categories = len(CATEGORY_DESCRIPTIONS)

        cat_lines = []
        for label, count, desc in CATEGORY_DESCRIPTIONS:
            cat_lines.append(f"- **{label}** ({count} tools): {desc}")
        categories_text = "\n".join(cat_lines)

        return f"""# Tallyfy MCP Server Capabilities

## Supported Capabilities

{format_capabilities(capabilities)}

## Tools ({tool_count} tools across {num_categories} categories)

{categories_text}

## Authentication

- **OAuth 2.1**: Authorization Code Flow + PKCE
- **JWT Validation**: RS256 signature verification
- **Issuer**: https://go.tallyfy.com
- **Scopes**: USERS_READ, TASKS_READ, TASKS_WRITE, PROCESSES_READ, PROCESSES_WRITE, TEMPLATES_READ, TEMPLATES_WRITE

## Transport

- **Protocol**: MCP (Model Context Protocol) v1.0
- **Transport**: Streamable HTTP with `/mcp/messages` endpoint
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
