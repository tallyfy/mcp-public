"""
Prometheus Metrics Route

Provides metrics endpoint for monitoring with security (IP whitelist + Basic Auth).
"""

import os
import logging
import base64
import secrets
from ipaddress import ip_address, ip_network

from starlette.responses import JSONResponse, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from constants import METRICS_ALLOWED_IPS, METRICS_USERNAME, METRICS_PASSWORD


# The IP-resolution logic below was written here for #219 (the /metrics
# allowlist was a no-op because request.client.host is loopback behind the
# tunnel). It now has three callers -- this allowlist, the rate limiter (#864)
# and the OAuth proxy's upstream forwarding (#863) -- so it lives in
# server/utils/client_ip.py and is imported here.
#
# The private names are re-exported unchanged because
# tests/unit/server/routes/test_metrics_ip_resolution.py imports them from this
# module by name. That test IS #219's regression coverage; keeping the aliases
# means the promotion cannot quietly drop it.
from utils.client_ip import (  # noqa: F401
    is_trusted_proxy as _is_trusted_proxy,
    resolve_client_ip as _resolve_client_ip,
    _TRUSTED_PROXY_NETWORKS,
)


def register_metrics_routes(mcp):
    """Register Prometheus metrics route with the MCP server."""

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics_endpoint(request):
        """
        Prometheus metrics endpoint for monitoring.
        Returns metrics in Prometheus exposition format.

        Security: IP whitelist + Basic Authentication
        """
        # Step 1: IP Whitelist Check
        if not METRICS_ALLOWED_IPS:
            logging.error("METRICS_ALLOWED_IPS environment variable not set")
            return JSONResponse(
                {"error": "Metrics endpoint not configured"},
                status_code=503
            )

        allowed_ips = METRICS_ALLOWED_IPS.split(',')
        client_ip = _resolve_client_ip(request)

        ip_allowed = False
        for allowed_ip in allowed_ips:
            allowed_ip = allowed_ip.strip()
            if '/' in allowed_ip:
                # CIDR notation
                try:
                    if ip_address(client_ip) in ip_network(allowed_ip):
                        ip_allowed = True
                        break
                except ValueError:
                    continue
            elif client_ip == allowed_ip:
                ip_allowed = True
                break

        if not ip_allowed:
            logging.warning(f"Metrics access denied for IP: {client_ip}")
            return Response(content="Forbidden", status_code=403)

        # Step 2: Basic Authentication Check
        if not METRICS_PASSWORD:
            logging.error("METRICS_PASSWORD environment variable not set - metrics endpoint disabled")
            return JSONResponse(
                {"error": "Metrics endpoint not configured"},
                status_code=503
            )

        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Basic '):
            return Response(
                content="Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Metrics"'}
            )

        # Verify credentials
        try:
            encoded_credentials = auth_header[6:]  # Remove "Basic " prefix
            decoded_bytes = base64.b64decode(encoded_credentials)
            decoded_credentials = decoded_bytes.decode('utf-8')
            provided_username, provided_password = decoded_credentials.split(':', 1)

            # Use constant-time comparison to prevent timing attacks
            username_match = secrets.compare_digest(provided_username, METRICS_USERNAME)
            password_match = secrets.compare_digest(provided_password, METRICS_PASSWORD)

            if not (username_match and password_match):
                logging.warning(f"Failed metrics authentication from {client_ip}")
                return Response(
                    content="Unauthorized",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Metrics"'}
                )
        except Exception as e:
            logging.warning(f"Invalid authorization header from {client_ip}: {e}")
            return Response(
                content="Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Metrics"'}
            )

        # Authentication successful, return metrics
        try:
            return Response(
                content=generate_latest(),
                media_type=CONTENT_TYPE_LATEST
            )
        except Exception as e:
            logging.error(f"Metrics endpoint failed: {e}")
            return JSONResponse(
                {"error": "Failed to retrieve metrics"},
                status_code=500
            )