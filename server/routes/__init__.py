"""
Routes module for MCP Server

Organizes HTTP routes and MCP resources into separate modules.
"""

from routes.oauth import register_oauth_routes
from routes.health import register_health_routes
from routes.metrics import register_metrics_routes
from routes.resources import register_resources
from routes.privacy import register_privacy_routes
from routes.capabilities import register_capabilities
from routes.landing import register_landing_routes
from routes.openai_apps_challenge import register_openai_apps_challenge_routes
from routes.server_card import register_server_card_routes
from routes.glama_card import register_glama_card_routes
from routes.favicon import register_favicon_routes


def register_all_routes(mcp):
    """Register all routes and resources with the MCP server."""
    register_oauth_routes(mcp)
    register_health_routes(mcp)
    register_metrics_routes(mcp)
    register_resources(mcp)
    register_privacy_routes(mcp)
    register_capabilities(mcp)
    register_landing_routes(mcp)
    register_openai_apps_challenge_routes(mcp)
    register_server_card_routes(mcp)
    register_glama_card_routes(mcp)
    register_favicon_routes(mcp)


__all__ = [
    "register_all_routes",
    "register_oauth_routes",
    "register_health_routes",
    "register_metrics_routes",
    "register_resources",
    "register_privacy_routes",
    "register_capabilities",
    "register_landing_routes",
    "register_openai_apps_challenge_routes",
    "register_server_card_routes",
    "register_glama_card_routes",
    "register_favicon_routes",
]