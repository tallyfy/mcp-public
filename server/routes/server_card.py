"""
Server Card Route (Smithery / external registries)

Serves an MCP server card at `/.well-known/mcp/server-card.json` so that
external registry scanners (Smithery, etc.) can populate display name,
description, authentication info, and icons without needing to authenticate
to introspect the OAuth-gated MCP transport.

The card schema follows Smithery's published convention; superset fields
(displayName, description, iconUrl, repository, etc.) are tolerated by
scanners that don't recognize them and used by those that do.

The CONTENT is static. The HOST is not: every URL that names this server is
resolved per request from the same helper the OAuth discovery documents use,
so the two documents cannot answer for different hosts (issues #1139, #1141).

References:
- Smithery docs: https://smithery.ai/docs/build/external
"""

from typing import Any, Dict

from starlette.responses import JSONResponse

from constants import MCP_RESOURCE_URL, SERVER_VERSION
# _get_base_url is imported, not re-implemented, and that is the whole point of
# issue #1139. The OAuth discovery document resolves its `issuer` through this
# exact function, so routing the card through the same call is what makes the
# two documents incapable of naming different hosts. A second copy of the
# host-resolution logic here would be a second thing to keep correct, and the
# copy that drifts is always the one nobody is looking at.
from routes.oauth import SUPPORTED_SCOPES, _get_base_url


def build_server_card(base_url: str) -> Dict[str, Any]:
    """Build the server card for one request, against the host that was asked.

    ``base_url`` comes from ``routes.oauth._get_base_url(request)``, the same
    resolver the OAuth discovery documents use: an allowlisted
    ``X-Forwarded-Host`` or ``Host`` wins, anything else falls back to
    ``MCP_RESOURCE_URL``.

    WHY THIS IS A FUNCTION AND NOT A MODULE-LEVEL DICT (issue #1139). It used
    to be a dict built once at import, so it was frozen to ``MCP_RESOURCE_URL``
    for the life of the process while the discovery document was rebuilt per
    request. On the canonical host the two agreed and every test stayed green.
    On any OTHER allowlisted host they disagreed: measured 2026-09-02 against
    the running staging server, ``X-Forwarded-Host: chat.tallyfy.com`` gave a
    discovery ``issuer`` of ``https://chat.tallyfy.com`` while the card still
    said ``https://staging.mcp.tallyfy.com``. A fabricated host, the control,
    made both fall back, which is what proved the probe was reading host
    reflection rather than noise.

    A fresh dict per call is deliberate: the response document is never shared,
    so a caller cannot mutate the card another request will serve.
    """
    return {
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
                # document names this same base_url as its `issuer`, and that
                # same server proxies /mcp/oauth/authorize with a 302 to
                # Tallyfy's account.tallyfy.com upstream, forwarding /register
                # and /token over HTTP. It used to hardcode go.tallyfy.com here,
                # a second literal that drifted from the real issuer --
                # go.tallyfy.com serves the legacy web app and answers OAuth
                # requests with an HTML page, not JSON (issue #1133).
                "authorizationServer": base_url,
                # Same reasoning as authorizationServer above, and #1136 is what
                # happens when only one of the three is derived. These two stayed
                # hardcoded to the production hostname, so on staging the card told
                # a scanner to fetch PRODUCTION's discovery document and named
                # PRODUCTION as the resource, while oauth-protected-resource and
                # oauth-authorization-server on that same host both correctly said
                # staging. Invisible in production, because there the literal
                # happens to equal the real value.
                "discoveryUrl": f"{base_url}/.well-known/oauth-authorization-server",
                "resource": base_url,
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
                # Issue #1141: this was the LAST hardcoded
                # "https://mcp.tallyfy.com/" in the card, one field below the
                # three #1136 fixed, and it survived that fix because the fix
                # asserted a hand-written list of field names. So staging's
                # card told a client to open a transport against PRODUCTION
                # while the OAuth fields on the same document said staging.
                # Measured on the running servers 2026-09-02, with the already
                # fixed `resource` field as the control that made it readable.
                # test_every_mcp_url_in_the_card_follows_the_deployment now
                # WALKS the document instead of naming fields, so the next
                # literal cannot survive the same way.
                "url": f"{base_url}/",
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
            "toolCount": 117,
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


# The card as it reads for this deployment's own canonical hostname.
#
# Kept as a module-level document because it is what the content drift guards
# read (tool counts, scopes, branding) and what the no-dashes walk in
# test_published_text_has_no_dashes.py recurses into. It is NOT what a request
# is answered with: the route below builds a fresh card against the host the
# client actually asked for, which is issue #1139.
_SERVER_CARD = build_server_card(MCP_RESOURCE_URL)


def register_server_card_routes(mcp):
    """Register the server card route at /.well-known/mcp/server-card.json."""

    @mcp.custom_route("/.well-known/mcp/server-card.json", methods=["GET"])
    async def server_card(request):
        return JSONResponse(build_server_card(_get_base_url(request)))
