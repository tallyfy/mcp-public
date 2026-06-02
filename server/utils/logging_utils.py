"""Enhanced logging utilities for MCP Server.

Provides detailed request/response logging with configurable verbosity.
Adapted from proxy/logging_utils.py for the consolidated MCP server.
"""

import logging
import json
import os
from typing import Dict, Any, Optional
from constants import LOG_VERBOSITY

logger = logging.getLogger(__name__)

# Log verbosity levels (from environment)
# 0 = Basic (default Uvicorn logs)
# 1 = Standard (method, path, status, timing, auth info)
# 2 = Detailed (+ headers, query params, session IDs)
# 3 = Debug (+ request/response bodies)



def mask_sensitive_data(value: str, show_chars: int = 8) -> str:
    """
    Mask sensitive data showing only first N characters.

    Args:
        value: The sensitive string to mask
        show_chars: Number of characters to show at the start

    Returns:
        Masked string with first show_chars visible
    """
    if not value or len(value) <= show_chars:
        return value[:4] + "..." if len(value) > 4 else "***"
    return f"{value[:show_chars]}...{value[-4:]}"


def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Sanitize headers to mask sensitive data.

    Args:
        headers: Dictionary of HTTP headers

    Returns:
        Sanitized headers with sensitive values masked
    """
    sensitive_keys = {'authorization', 'x-api-key', 'api-key', 'cookie', 'set-cookie'}
    sanitized = {}

    for key, value in headers.items():
        key_lower = key.lower()
        if key_lower in sensitive_keys:
            if key_lower == 'authorization' and value.startswith('Bearer '):
                sanitized[key] = f"Bearer {mask_sensitive_data(value[7:])}"
            else:
                sanitized[key] = mask_sensitive_data(value)
        else:
            sanitized[key] = value

    return sanitized


def format_body_preview(body: bytes, max_length: int = 500) -> str:
    """
    Format request/response body for logging.

    Args:
        body: Raw body bytes
        max_length: Maximum length to display

    Returns:
        Formatted body string for logging
    """
    if not body:
        return "<empty>"

    try:
        # Try to parse as JSON for better formatting
        body_str = body.decode('utf-8')
        if body_str.startswith('{') or body_str.startswith('['):
            data = json.loads(body_str)
            formatted = json.dumps(data, indent=2)
            if len(formatted) > max_length:
                return formatted[:max_length] + f"... (truncated {len(formatted) - max_length} chars)"
            return formatted
        else:
            # Plain text
            if len(body_str) > max_length:
                return body_str[:max_length] + f"... (truncated {len(body_str) - max_length} chars)"
            return body_str
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"<binary data, {len(body)} bytes>"


def log_request_details(
    method: str,
    path: str,
    query_params: str,
    headers: Dict[str, str],
    body: Optional[bytes] = None,
    client_ip: Optional[str] = None
):
    """
    Log detailed request information based on verbosity level.

    Args:
        method: HTTP method (GET, POST, etc.)
        path: Request path
        query_params: Query string
        headers: Request headers
        body: Request body (optional)
        client_ip: Client IP address (optional)
    """
    if LOG_VERBOSITY < 1:
        return

    # Level 1: Basic info
    log_parts = [f"→ {method} {path}"]

    if client_ip:
        log_parts.append(f"client={client_ip}")

    # Level 2: Headers and query params
    if LOG_VERBOSITY >= 2:
        if query_params:
            log_parts.append(f"query={query_params}")

        # Log important headers
        sanitized = sanitize_headers(headers)
        important_headers = {
            k: v for k, v in sanitized.items()
            if k.lower() in ['content-type', 'accept', 'mcp-session-id',
                            'authorization', 'x-org-id', 'x-organization-id']
        }
        if important_headers:
            headers_str = " | ".join(f"{k}={v}" for k, v in important_headers.items())
            log_parts.append(f"headers=[{headers_str}]")

    # Level 3: Request body
    if LOG_VERBOSITY >= 3 and body:
        body_preview = format_body_preview(body)
        logger.debug(f"{' | '.join(log_parts)}\n  Body: {body_preview}")
        return

    logger.info(" | ".join(log_parts))


def log_response_details(
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None
):
    """
    Log detailed response information based on verbosity level.

    Args:
        method: HTTP method
        path: Request path
        status_code: HTTP response status code
        duration_ms: Request duration in milliseconds
        headers: Response headers (optional)
        body: Response body (optional)
    """
    if LOG_VERBOSITY < 1:
        return

    # Level 1: Basic info
    status_emoji = "✅" if 200 <= status_code < 300 else "⚠️" if status_code < 400 else "❌"
    log_parts = [f"← {method} {path}", f"{status_emoji} {status_code}", f"{duration_ms:.1f}ms"]

    # Level 2: Response headers
    if LOG_VERBOSITY >= 2 and headers:
        important_headers = {
            k: v for k, v in headers.items()
            if k.lower() in ['content-type', 'mcp-session-id', 'cache-control']
        }
        if important_headers:
            headers_str = " | ".join(f"{k}={v}" for k, v in important_headers.items())
            log_parts.append(f"headers=[{headers_str}]")

    # Level 3: Response body
    if LOG_VERBOSITY >= 3 and body:
        body_preview = format_body_preview(body)
        logger.debug(f"{' | '.join(log_parts)}\n  Response: {body_preview}")
        return

    logger.info(" | ".join(log_parts))


def log_authentication(
    success: bool,
    auth_type: str,
    user_id: Optional[str] = None,
    org_id: Optional[str] = None,
    scopes: Optional[list] = None,
    error: Optional[str] = None,
    client_ip: Optional[str] = None
):
    """
    Log authentication attempt with details.

    Args:
        success: Whether authentication succeeded
        auth_type: Type of authentication (oauth, jwt, etc.)
        user_id: User ID if available
        org_id: Organization ID if available
        scopes: OAuth scopes if available
        error: Error message if authentication failed
        client_ip: Client IP address
    """
    if LOG_VERBOSITY < 1:
        return

    log_parts = ["🔐 Auth"]

    if client_ip:
        log_parts.append(f"client={client_ip}")

    log_parts.append(f"type={auth_type}")

    if success:
        log_parts.append("✅ SUCCESS")
        if user_id:
            log_parts.append(f"user={mask_sensitive_data(user_id, 8)}")
        if org_id:
            log_parts.append(f"org={mask_sensitive_data(org_id, 8)}")
        if LOG_VERBOSITY >= 2 and scopes:
            log_parts.append(f"scopes={scopes}")
        logger.info(" | ".join(log_parts))
    else:
        log_parts.append("❌ FAILED")
        if error:
            log_parts.append(f"error={error}")
        logger.warning(" | ".join(log_parts))


def log_mcp_protocol(
    method: str,
    mcp_method: Optional[str] = None,
    session_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None
):
    """
    Log MCP protocol-specific information.

    Args:
        method: HTTP method
        mcp_method: MCP method being called (tools/call, resources/list, etc.)
        session_id: MCP session ID
        params: MCP request parameters
    """
    if LOG_VERBOSITY < 2:
        return

    log_parts = [f"🔧 MCP {method}"]

    if mcp_method:
        log_parts.append(f"method={mcp_method}")

    if session_id:
        log_parts.append(f"session={mask_sensitive_data(session_id, 12)}")

    if LOG_VERBOSITY >= 3 and params:
        params_str = json.dumps(params, indent=2)[:200]
        logger.debug(f"{' | '.join(log_parts)}\n  Params: {params_str}")
    else:
        logger.info(" | ".join(log_parts))


def log_oauth_flow(
    step: str,
    client_id: Optional[str] = None,
    state: Optional[str] = None,
    code: Optional[str] = None,
    details: Optional[str] = None
):
    """
    Log OAuth 2.1 flow steps.

    Args:
        step: OAuth flow step (authorize, token, etc.)
        client_id: OAuth client ID
        state: OAuth state parameter
        code: Authorization code (if applicable)
        details: Additional details
    """
    if LOG_VERBOSITY < 2:
        return

    log_parts = [f"🔑 OAuth: {step}"]

    if client_id:
        log_parts.append(f"client={mask_sensitive_data(client_id, 12)}")

    if state:
        log_parts.append(f"state={mask_sensitive_data(state, 8)}")

    if code:
        log_parts.append(f"code={mask_sensitive_data(code, 8)}")

    if details:
        log_parts.append(details)

    logger.info(" | ".join(log_parts))
