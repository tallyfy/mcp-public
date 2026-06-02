"""
Health Check Routes

Provides health and readiness endpoints for monitoring and load balancers.
"""

import logging

from starlette.responses import JSONResponse


def register_health_routes(mcp):
    """Register health check routes with the MCP server."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        """
        Health check endpoint for monitoring.
        Returns basic server status information.
        """
        return JSONResponse({
            "status": "healthy"
        })

    @mcp.custom_route("/ready", methods=["GET"])
    async def readiness_check(request):
        """
        Readiness check endpoint for load balancers.
        Verifies that the server is ready to accept requests.
        """
        try:
            return JSONResponse({
                "status": "ready"
            })
        except Exception as e:
            logging.error(f"Readiness check failed: {e}")
            return JSONResponse(
                {"status": "not_ready", "error": "Server initialization incomplete"},
                status_code=503
            )