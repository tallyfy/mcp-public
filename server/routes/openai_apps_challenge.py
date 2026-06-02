"""
OpenAI Apps Domain Verification Route

Serves the verification token at `/.well-known/openai-apps-challenge` so the
OpenAI ChatGPT Apps platform can verify domain ownership of mcp.tallyfy.com.

The token is sourced from the `OPENAI_APPS_CHALLENGE_TOKEN` environment
variable. If unset, the endpoint returns 404 — this avoids advertising a
challenge when none has been issued by OpenAI.

Refs: tallyfy/mcp#119 (OpenAI ChatGPT App submission).
"""

import logging
import os

from starlette.responses import PlainTextResponse, Response


def _get_token() -> str:
    """Return the configured challenge token, or empty string if unset."""
    return os.getenv("OPENAI_APPS_CHALLENGE_TOKEN", "").strip()


def register_openai_apps_challenge_routes(mcp):
    """Register the domain-verification route with the MCP server."""

    @mcp.custom_route("/.well-known/openai-apps-challenge", methods=["GET"])
    async def openai_apps_challenge(request):
        token = _get_token()
        if not token:
            logging.warning(
                "OpenAI Apps challenge requested but OPENAI_APPS_CHALLENGE_TOKEN is unset"
            )
            return Response(status_code=404)
        return PlainTextResponse(token, media_type="text/plain")
