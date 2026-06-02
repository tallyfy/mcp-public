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


# Trusted proxy networks. Only requests arriving from one of these peer
# addresses are allowed to supply the true caller IP via CF-Connecting-IP /
# X-Forwarded-For. In Tallyfy's production topology the tunnel terminates on
# the same host, so request.client.host is loopback; we deliberately do NOT
# accept public CF ranges here because that would let any caller claim any IP
# by asserting a CF header directly.
_TRUSTED_PROXY_NETWORKS = [
    ip_network("127.0.0.0/8"),
    ip_network("::1/128"),
    ip_network("10.0.0.0/8"),      # Docker default bridge + compose networks
    ip_network("172.16.0.0/12"),   # Docker's range for user-defined networks
    ip_network("192.168.0.0/16"),  # Private LAN
]


def _is_trusted_proxy(peer_ip: str) -> bool:
    try:
        ip = ip_address(peer_ip)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_PROXY_NETWORKS)


def _resolve_client_ip(request) -> str:
    """Return the true originating IP, honoring CF-Connecting-IP only when
    the immediate peer is a trusted proxy. See issue #219.

    Fallback order when peer is trusted:
      1. CF-Connecting-IP (set by Cloudflare at the edge — single value)
      2. X-Forwarded-For (first entry — least trusted of the three)
      3. request.client.host (the proxy itself; fine for debugging)

    When peer is untrusted we always use request.client.host and ignore any
    spoofed forwarding headers.
    """
    peer_ip = request.client.host if request.client else ""
    if peer_ip and _is_trusted_proxy(peer_ip):
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return cf_ip
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    return peer_ip


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