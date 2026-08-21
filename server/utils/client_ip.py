"""
Originating-client IP resolution, shared across the server.

This logic was written for the /metrics IP allowlist (issue #219) and lived in
server/routes/metrics.py. It is promoted here unchanged because three callers
now need the same answer, and three copies of a security predicate is how they
drift apart:

  - server/routes/metrics.py      -- the /metrics allowlist (the original)
  - server/middleware/rate_limit.py -- per-caller bucketing (issue #864)
  - server/routes/oauth.py        -- forwarding the caller IP upstream (#863)

THE RULE, and why it is shaped this way. In Tallyfy's production topology the
Cloudflare tunnel terminates on the same host, so ``request.client.host`` is
loopback for every request and is useless as a caller identity on its own. The
true caller arrives in ``CF-Connecting-IP``, set by Cloudflare at the edge.

But a forwarding header is only as trustworthy as the peer that sent it. We
therefore honour it ONLY when the immediate peer is loopback/RFC1918 -- i.e.
the tunnel or a sibling container. We deliberately do NOT trust public
Cloudflare ranges: doing so would let any caller on the internet claim any IP
simply by asserting a CF header directly at us, which is precisely the bypass
#219 was filed for.

When the peer is untrusted we ignore every forwarding header and use the socket
address. That fails toward "attribute this request to whoever actually
connected", which is the safe direction for both an allowlist and a rate limit.
"""

from ipaddress import ip_address, ip_network

# Peer addresses whose forwarding headers we believe. Loopback covers the
# same-host tunnel; the RFC1918 ranges cover Docker's default bridge, Docker
# user-defined networks, and a private LAN.
_TRUSTED_PROXY_NETWORKS = [
    ip_network("127.0.0.0/8"),
    ip_network("::1/128"),
    ip_network("10.0.0.0/8"),      # Docker default bridge + compose networks
    ip_network("172.16.0.0/12"),   # Docker's range for user-defined networks
    ip_network("192.168.0.0/16"),  # Private LAN
]


def is_trusted_proxy(peer_ip: str) -> bool:
    """Whether a forwarding header from this peer address may be believed."""
    try:
        ip = ip_address(peer_ip)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_PROXY_NETWORKS)


def resolve_client_ip(request) -> str:
    """Return the true originating IP, honoring CF-Connecting-IP only when
    the immediate peer is a trusted proxy. See issue #219.

    Fallback order when peer is trusted:
      1. CF-Connecting-IP (set by Cloudflare at the edge -- single value)
      2. X-Forwarded-For (first entry -- least trusted of the three)
      3. request.client.host (the proxy itself; fine for debugging)

    When peer is untrusted we always use request.client.host and ignore any
    spoofed forwarding headers.
    """
    peer_ip = request.client.host if request.client else ""
    if peer_ip and is_trusted_proxy(peer_ip):
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return cf_ip
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    return peer_ip


def resolve_forwardable_client_ip(request) -> str:
    """The caller IP only when we are entitled to assert it to someone else.

    ``resolve_client_ip`` always returns something, falling back to the socket
    address, because an allowlist and a rate limiter both need an answer even
    for a direct caller. Forwarding is different: telling api-v2 "the client
    was X" when X is really our own egress address is worse than saying
    nothing, because it invites the upstream to bucket every proxied request
    together -- which is the exact defect #863 exists to fix.

    So this returns "" unless the peer is trusted AND a forwarding header
    actually carried a caller identity. The caller then omits the header
    entirely and the upstream falls back to its own socket-level view.
    """
    peer_ip = request.client.host if request.client else ""
    if not peer_ip or not is_trusted_proxy(peer_ip):
        return ""
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return ""
