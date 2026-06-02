"""
Glama Connector Verification Route

Serves a static `/.well-known/glama.json` file so Glama (glama.ai) can verify
domain ownership of the Tallyfy MCP connector at
`https://glama.ai/mcp/connectors/com.tallyfy/mcp-server`.

Per Glama's claim flow (visible on the connector page under "How do I verify
ownership of this connector?"), publishing this JSON file with a maintainer
email that matches a Glama account email completes the claim within minutes.

References:
- Glama listing: https://glama.ai/mcp/connectors/com.tallyfy/mcp-server
- Glama schema: https://glama.ai/mcp/schemas/connector.json
"""

from starlette.responses import JSONResponse


_GLAMA_CARD = {
    "$schema": "https://glama.ai/mcp/schemas/connector.json",
    "maintainers": [
        {"email": "amit@tallyfy.com"},
    ],
}


def register_glama_card_routes(mcp):
    """Register the Glama connector verification route at /.well-known/glama.json."""

    @mcp.custom_route("/.well-known/glama.json", methods=["GET"])
    async def glama_card(request):
        return JSONResponse(_GLAMA_CARD)
