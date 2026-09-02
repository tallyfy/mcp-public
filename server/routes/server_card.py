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

from constants import MCP_RESOURCE_URL, SERVER_VERSION
from routes.oauth import SUPPORTED_SCOPES


_SERVER_CARD = {
    "serverInfo": {
        "name": "Tallyfy Workflow Automation",
        "version": SERVER_VERSION,
    },
    "displayName": "Tallyfy Workflow Automation",
    "description": (
        "Run your operations from your AI assistant. Launch workflows, complete "
        "tasks, manage approvals, and update templates in Tallyfy, all from "
        "natural conversation."
    ),
    "tagline": "Automate tasks, processes, and approvals with AI.",
    # Kept in sync with the "Try these" list in constants.INSTRUCTIONS_TEMPLATE
    # and the landing page -- tests/unit/server/test_server_instructions.py
    # pins all three surfaces to the same five prompts.
    "examplePrompts": [
        "Turn this SOP document into a runnable Tallyfy template",
        "Launch our client-onboarding process for Acme Corp",
        "What did my team complete this week?",
        "Build a process for handling customer refunds and test it with me",
        (
            "Ask 8 people to confirm their off-site attendance by Friday"
            " and track who has answered"
        ),
    ],
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
            # This MCP server IS the authorization server a client talks to:
            # routes/oauth.py's own /.well-known/oauth-authorization-server
            # document names MCP_RESOURCE_URL as its `issuer` (via
            # _get_base_url()'s fallback), and that same server proxies
            # /mcp/oauth/authorize with a 302 to Tallyfy's account.tallyfy.com
            # upstream, forwarding /register and /token over HTTP. It used to
            # hardcode go.tallyfy.com here, a second literal that drifted from
            # the real issuer -- go.tallyfy.com serves the legacy web app and
            # answers OAuth requests with an HTML page, not JSON (issue #1133).
            # Derive from the same constant so the two documents cannot
            # disagree again.
            "authorizationServer": MCP_RESOURCE_URL,
            # Same reasoning as authorizationServer above, and #1136 is what
            # happens when only one of the three is derived. These two stayed
            # hardcoded to the production hostname, so on staging the card told
            # a scanner to fetch PRODUCTION's discovery document and named
            # PRODUCTION as the resource, while oauth-protected-resource and
            # oauth-authorization-server on that same host both correctly said
            # staging. Invisible in production, because there the literal
            # happens to equal the real value.
            "discoveryUrl": f"{MCP_RESOURCE_URL}/.well-known/oauth-authorization-server",
            "resource": MCP_RESOURCE_URL,
            # Derived from the OAuth discovery document, never hand-listed.
            # A literal list here silently under-declares the moment a scope is
            # added: this advertised 6 of 12 for months (issue #860), and it is
            # the artifact directory reviewers read. See test_server_card.py.
            "scopes": list(SUPPORTED_SCOPES),
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
        # Kept static deliberately. Smithery's scanner reads this as a fixed
        # document. Drift is prevented by a test, not by computing it here:
        # tests/unit/server/routes/test_server_card.py asserts these values
        # against routes.capabilities.category_breakdown(), which counts the
        # tools each module actually registers. Update both or neither.
        "toolCount": 113,
        "toolCategories": 15,
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
            "template_mapping_validation",
            "api_fallback",
            "org_context",
        ],
    },
}


def register_server_card_routes(mcp):
    """Register the static server card route at /.well-known/mcp/server-card.json."""

    @mcp.custom_route("/.well-known/mcp/server-card.json", methods=["GET"])
    async def server_card(request):
        return JSONResponse(_SERVER_CARD)
