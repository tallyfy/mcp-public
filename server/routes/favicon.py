"""
Favicon route.

mcp.tallyfy.com serves no favicon, so Google's favicon service (and therefore the
logo Claude shows for tool calls and in the Connectors Directory) falls back to a
generic globe. Redirect /favicon.ico to tallyfy.com's favicon so the Tallyfy logo
is resolved. Registered before the catch-all 404 route in server.py.
"""

from starlette.responses import RedirectResponse

_FAVICON_URL = "https://tallyfy.com/favicon.ico"


def register_favicon_routes(mcp):
    """Register /favicon.ico -> tallyfy.com favicon (301)."""

    @mcp.custom_route("/favicon.ico", methods=["GET", "HEAD"])
    async def favicon(request):
        return RedirectResponse(_FAVICON_URL, status_code=301)
