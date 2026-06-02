"""
Static Server Card Route (Smithery / external registries)

Serves a manual MCP server card at `/.well-known/mcp/server-card.json` so
that external registry scanners (Smithery, etc.) can populate display name,
description, authentication info, and icons without needing to authenticate
to introspect the OAuth-gated MCP transport.

The card schema follows Smithery's published convention; superset fields
(displayName, description, iconUrl, repository, etc.) are tolerated by
scanners that don't recognize them and used by those that do.

References:
- Smithery docs: https://smithery.ai/docs/build/external
"""

from starlette.responses import JSONResponse


_SERVER_CARD = {
    "serverInfo": {
        "name": "Tallyfy Workflow Automation",
        "version": "1.0.0",
    },
    "displayName": "Tallyfy Workflow Automation",
    "description": (
        "Run your operations from your AI assistant. Launch workflows, complete "
        "tasks, manage approvals, and update templates in Tallyfy — all from "
        "natural conversation."
    ),
    "tagline": "Automate tasks, processes, and approvals with AI.",
    "category": "productivity",
    "iconUrl": "https://tallyfy.com/tallyfy-logo-icon.svg",
    "logoUrl": "https://tallyfy.com/tallyfy-logo-icon.svg",
    "homepage": "https://tallyfy.com/products/pro/integrations/mcp-server/",
    "documentation": "https://tallyfy.com/products/pro/integrations/mcp-server/",
    "repository": "https://github.com/tallyfy/mcp",
    "supportEmail": "support@tallyfy.com",
    "privacyPolicy": "https://tallyfy.com/legal/privacy-policy/",
    "termsOfService": "https://tallyfy.com/legal/",
    "authentication": {
        "required": True,
        "schemes": ["oauth2"],
        "oauth2": {
            "authorizationServer": "https://go.tallyfy.com",
            "discoveryUrl": "https://mcp.tallyfy.com/.well-known/oauth-authorization-server",
            "resource": "https://mcp.tallyfy.com",
            "scopes": [
                "mcp.tasks.read",
                "mcp.tasks.write",
                "mcp.processes.read",
                "mcp.processes.write",
                "mcp.templates.read",
                "mcp.templates.write",
            ],
        },
    },
    "transports": [
        {
            "type": "streamable-http",
            "url": "https://mcp.tallyfy.com/",
        }
    ],
    "capabilities": {
        "tools": True,
        "resources": True,
        "prompts": False,
        "logging": False,
        "completions": False,
        "tasks": False,
    },
    "summary": {
        "toolCount": 108,
        "toolCategories": 12,
        "categories": [
            "user_management",
            "task_management",
            "process_management",
            "template_management",
            "form_fields",
            "search",
            "automation",
            "group_management",
            "comment_management",
            "tag_management",
            "folder_management",
            "user_interaction",
        ],
    },
}


def register_server_card_routes(mcp):
    """Register the static server card route at /.well-known/mcp/server-card.json."""

    @mcp.custom_route("/.well-known/mcp/server-card.json", methods=["GET"])
    async def server_card(request):
        return JSONResponse(_SERVER_CARD)
