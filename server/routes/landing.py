"""
Landing Page Route

Serves a public-facing HTML landing page at `GET /info` (with `/welcome`
and `/about` aliases) for humans who visit mcp.tallyfy.com in a browser.

Why a dedicated path instead of `GET /`: FastMCP's streamable-http MCP
transport at `path='/'` is registered before our `custom_route` handlers
in the Starlette app, so the OAuth challenge always wins for `GET /`
regardless of `Accept` headers. Putting the landing under `/info` keeps
the MCP transport untouched for clients (Claude, ChatGPT, Cursor, etc.)
while still giving humans a real page to land on.

Closes #433.
"""

from starlette.responses import HTMLResponse


def register_landing_routes(mcp):
    """Register the public landing page route with the MCP server."""

    @mcp.custom_route("/info", methods=["GET"])
    async def landing_info(request):
        host = request.headers.get("host", "")
        return HTMLResponse(_render_landing_for_host(host))

    @mcp.custom_route("/welcome", methods=["GET"])
    async def landing_welcome(request):
        host = request.headers.get("host", "")
        return HTMLResponse(_render_landing_for_host(host))

    @mcp.custom_route("/about", methods=["GET"])
    async def landing_about(request):
        host = request.headers.get("host", "")
        return HTMLResponse(_render_landing_for_host(host))


_GCP_BANNER = """
    <div class="gcp-banner" role="note">
        <strong>Google Cloud Run mirror.</strong>
        This endpoint (<code>mcp-gcp.tallyfy.com</code>) is the Tier-1 mirror for
        <strong>Google Gemini Enterprise</strong> deployments. Same tools, same OAuth.
        <a href="https://tallyfy.com/products/pro/integrations/mcp-server/google-gemini/">Gemini setup guide &rarr;</a>
    </div>
"""


def _render_landing_for_host(host: str) -> str:
    """Render the landing HTML, injecting a Cloud Run banner on mcp-gcp.tallyfy.com."""
    banner = _GCP_BANNER if "mcp-gcp" in (host or "").lower() else ""
    return _LANDING_HTML.replace("{GEMINI_BANNER}", banner)


_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tallyfy MCP Server — Workflow Automation for AI Assistants</title>
    <meta name="description" content="Run your operations from your AI assistant. The Tallyfy MCP server exposes 107 tools for workflow automation across Claude, ChatGPT, Cursor, and more.">
    <meta property="og:title" content="Tallyfy MCP Server">
    <meta property="og:description" content="Workflow automation for AI assistants. Connect Tallyfy to Claude, ChatGPT, Cursor, and any MCP-compatible client.">
    <meta property="og:image" content="https://tallyfy.com/images/press/tallyfy-logo.png">
    <meta property="og:type" content="website">
    <meta property="og:url" content="https://mcp.tallyfy.com/">
    <link rel="icon" href="https://tallyfy.com/favicon.ico">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --tallyfy-orange: #EE9A22;
            --tallyfy-blue: #2E5C9B;
            --tallyfy-green: #3FB65B;
            --ink: #1a1a1a;
            --muted: #5a5a5a;
            --border: #e5e5e5;
            --bg: #fff;
            --code-bg: #f6f8fa;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Inter, sans-serif;
            line-height: 1.6;
            color: var(--ink);
            background: var(--bg);
        }
        .container { max-width: 880px; margin: 0 auto; padding: 56px 24px 80px; }
        header { display: flex; align-items: center; gap: 16px; margin-bottom: 48px; }
        header img { width: 40px; height: 40px; }
        header .brand { font-weight: 600; font-size: 18px; color: var(--ink); }
        header .brand span { color: var(--muted); font-weight: 400; }
        h1 { font-size: 36px; line-height: 1.2; margin-bottom: 16px; letter-spacing: -0.02em; }
        .lead { font-size: 18px; color: var(--muted); margin-bottom: 36px; max-width: 640px; }
        h2 { font-size: 22px; margin-top: 44px; margin-bottom: 16px; letter-spacing: -0.01em; }
        h3 { font-size: 16px; margin-top: 24px; margin-bottom: 8px; font-weight: 600; }
        p { margin-bottom: 12px; color: var(--ink); }
        .endpoint {
            display: block;
            background: var(--code-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px 16px;
            font-family: 'SF Mono', Menlo, Monaco, Consolas, monospace;
            font-size: 14px;
            color: var(--tallyfy-blue);
            word-break: break-all;
            margin-bottom: 28px;
        }
        .badges {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin: 8px 0 32px;
        }
        .badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: var(--code-bg);
            border: 1px solid var(--border);
            border-radius: 999px;
            padding: 4px 12px;
            font-size: 13px;
            color: var(--muted);
            text-decoration: none;
        }
        .badge:hover { border-color: var(--tallyfy-blue); color: var(--tallyfy-blue); }
        .badge .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--tallyfy-green); }
        .clients {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 12px;
            margin: 16px 0 24px;
        }
        .client {
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px 16px;
        }
        .client h4 { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
        .client p { font-size: 13px; color: var(--muted); margin: 0; }
        a { color: var(--tallyfy-blue); text-decoration: none; }
        a:hover { text-decoration: underline; }
        .footer-links {
            display: flex;
            flex-wrap: wrap;
            gap: 8px 16px;
            margin-top: 28px;
            padding-top: 24px;
            border-top: 1px solid var(--border);
            font-size: 14px;
        }
        .footer-links a { color: var(--muted); }
        .footer-links a:hover { color: var(--tallyfy-blue); }
        code {
            background: var(--code-bg);
            padding: 1px 6px;
            border-radius: 4px;
            font-family: 'SF Mono', Menlo, Monaco, Consolas, monospace;
            font-size: 0.9em;
        }
        pre {
            background: var(--code-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px 16px;
            font-family: 'SF Mono', Menlo, Monaco, Consolas, monospace;
            font-size: 13px;
            overflow-x: auto;
            margin-bottom: 16px;
            line-height: 1.5;
        }
        .gcp-banner {
            background: #eef5ff;
            border: 1px solid #b8d4f5;
            border-left: 4px solid var(--tallyfy-blue);
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 24px;
            font-size: 14px;
            color: var(--ink);
        }
        .gcp-banner code { background: rgba(46, 92, 155, 0.08); }
        .gcp-banner a { font-weight: 600; }
        .carousel {
            position: relative;
            margin: 16px -24px 32px;
            padding: 0 24px;
        }
        .carousel-track {
            display: flex;
            gap: 16px;
            overflow-x: auto;
            scroll-snap-type: x mandatory;
            scroll-behavior: smooth;
            scrollbar-width: thin;
            scrollbar-color: var(--tallyfy-blue) transparent;
            padding-bottom: 12px;
        }
        .carousel-track::-webkit-scrollbar { height: 8px; }
        .carousel-track::-webkit-scrollbar-track { background: transparent; }
        .carousel-track::-webkit-scrollbar-thumb { background: var(--tallyfy-blue); border-radius: 4px; }
        .carousel-slide {
            flex: 0 0 88%;
            max-width: 720px;
            scroll-snap-align: start;
            display: block;
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
            background: var(--code-bg);
        }
        .carousel-slide img {
            display: block;
            width: 100%;
            height: auto;
            background: #f0f0f0;
            pointer-events: none;
            user-select: none;
        }
        .carousel-slide .caption {
            padding: 14px 18px;
            font-size: 14px;
            color: var(--muted);
            border-top: 1px solid var(--border);
            background: white;
        }
        .carousel-slide .caption strong {
            display: block;
            color: var(--ink);
            font-size: 15px;
            margin-bottom: 4px;
        }
        .carousel-hint {
            font-size: 13px;
            color: var(--muted);
            margin-bottom: 8px;
        }
        .carousel-nav {
            display: flex;
            gap: 8px;
            justify-content: flex-end;
            margin-top: 4px;
        }
        .carousel-nav button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            border: 1px solid var(--border);
            background: white;
            border-radius: 50%;
            color: var(--ink);
            font-size: 18px;
            cursor: pointer;
            font-family: inherit;
            transition: border-color 0.15s, color 0.15s, background 0.15s;
        }
        .carousel-nav button:hover {
            border-color: var(--tallyfy-blue);
            color: var(--tallyfy-blue);
            background: #f6f9fd;
        }
        .carousel-nav button:active { background: #eaf1fa; }
        .carousel-nav button:focus-visible { outline: 2px solid var(--tallyfy-blue); outline-offset: 2px; }
        @media (min-width: 720px) {
            .carousel-slide { flex: 0 0 75%; }
        }
        @media (max-width: 600px) {
            .container { padding: 36px 20px 60px; }
            h1 { font-size: 28px; }
            .lead { font-size: 16px; }
            .carousel { margin: 12px -20px 28px; padding: 0 20px; }
            .carousel-slide { flex: 0 0 92%; }
        }
    </style>
</head>
<body>

<div class="container">

    <header>
        <img src="https://tallyfy.com/tallyfy-logo-icon.svg" alt="Tallyfy" />
        <div class="brand">Tallyfy MCP Server <span>&middot; for AI assistants</span></div>
    </header>

    {GEMINI_BANNER}

    <h1>Run your operations from your AI assistant.</h1>

    <p class="lead">
        Launch workflows, complete tasks, manage approvals, and update templates in
        <a href="https://tallyfy.com/">Tallyfy</a> &mdash; all from natural conversation
        with Claude, ChatGPT, Cursor, or any MCP-compatible AI client.
    </p>

    <div class="badges">
        <span class="badge"><span class="dot"></span>Production &middot; live now</span>
        <a class="badge" href="https://registry.modelcontextprotocol.io/?q=tallyfy" target="_blank" rel="noopener">Listed on the Official MCP Registry</a>
        <span class="badge">107 tools across 12 categories</span>
        <span class="badge">OAuth 2.1 + DCR</span>
    </div>

    <h2>Endpoint</h2>
    <code class="endpoint">https://mcp.tallyfy.com/</code>

    <h2>Connect from your AI client</h2>

    <div class="clients">
        <div class="client">
            <h4>Claude Desktop &middot; claude.ai &middot; Claude Code</h4>
            <p>Add as a remote connector. <a href="https://tallyfy.com/products/pro/integrations/mcp-server/claude-anthropic/">Setup guide &rarr;</a></p>
        </div>
        <div class="client">
            <h4>ChatGPT &middot; OpenAI Apps</h4>
            <p>Custom connector or installed app. <a href="https://tallyfy.com/products/pro/integrations/mcp-server/openai-chatgpt/">Setup guide &rarr;</a></p>
        </div>
        <div class="client">
            <h4>Cursor &middot; Cline &middot; Continue</h4>
            <p>Add via standard MCP server config. <a href="https://tallyfy.com/products/pro/integrations/mcp-server/">Docs &rarr;</a></p>
        </div>
        <div class="client">
            <h4>Google Gemini CLI &middot; Gemini Enterprise</h4>
            <p>Configure as a custom MCP data store. <a href="https://tallyfy.com/products/pro/integrations/mcp-server/google-gemini/">Setup guide &rarr;</a></p>
        </div>
        <div class="client">
            <h4>Microsoft Copilot Studio</h4>
            <p>Connector wizard with OAuth 2.1. <a href="https://tallyfy.com/products/pro/integrations/mcp-server/microsoft-copilot-studio/">Setup guide &rarr;</a></p>
        </div>
        <div class="client">
            <h4>MCP Inspector &middot; CLI tools</h4>
            <p>Use the standard streamable-http transport.</p>
        </div>
    </div>

    <h2>What it looks like in Claude</h2>

    <p class="carousel-hint">Real screenshots from claude.ai with the Tallyfy connector active. Use the arrows below or swipe to see all four.</p>

    <div class="carousel" aria-label="Tallyfy MCP screenshots in Claude">
        <div class="carousel-track" id="carousel-track">
            <div class="carousel-slide">
                <img src="https://screenshots.tallyfy.com/tallyfy/pro/mcp-claude-template-creation.png" alt="Claude creating a 7-step Business Trip Request template in Tallyfy with 5 automation rules matching a flowchart" loading="lazy" />
                <div class="caption">
                    <strong>Build a workflow from a flowchart</strong>
                    Claude turns a dropped diagram into a 7-step template with 5 automation rules.
                </div>
            </div>
            <div class="carousel-slide">
                <img src="https://screenshots.tallyfy.com/tallyfy/pro/mcp-claude-clarifying-questions.png" alt="Claude using ask_user_question to clarify scope before querying pending tasks - Tallyfy connector shown as Connected" loading="lazy" />
                <div class="caption">
                    <strong>Confirm scope before pulling data</strong>
                    The <code>ask_user_question</code> tool clarifies ambiguous requests before any tools fire.
                </div>
            </div>
            <div class="carousel-slide">
                <img src="https://screenshots.tallyfy.com/tallyfy/pro/mcp-claude-process-review.png" alt="Claude reviewing a Client Onboarding process and confirming scope via a four-option ranked picker" loading="lazy" />
                <div class="caption">
                    <strong>Review an existing process</strong>
                    Claude uses <code>ask_user_to_rank</code> to pick suggest-only, edit-on-approval, or direct edits.
                </div>
            </div>
            <div class="carousel-slide">
                <img src="https://screenshots.tallyfy.com/tallyfy/pro/mcp-claude-overdue-analysis.png" alt="Claude chaining get_me, get_my_tasks, and counting with a progress sidebar showing 822 open tasks - 805 overdue and 17 auto-skipped" loading="lazy" />
                <div class="caption">
                    <strong>Multi-tool analysis with progress tracking</strong>
                    Claude chains <code>get_me</code>, <code>get_my_tasks</code>, plus counting and verification.
                </div>
            </div>
        </div>
        <div class="carousel-nav">
            <button type="button" aria-label="Previous screenshot" onclick="(function(t){t.scrollBy({left:-(t.querySelector('.carousel-slide').getBoundingClientRect().width+16),behavior:'smooth'})})(document.getElementById('carousel-track'))">&larr;</button>
            <button type="button" aria-label="Next screenshot" onclick="(function(t){t.scrollBy({left:(t.querySelector('.carousel-slide').getBoundingClientRect().width+16),behavior:'smooth'})})(document.getElementById('carousel-track'))">&rarr;</button>
        </div>
    </div>

    <h2>Try it from the command line</h2>

    <p>You'll need an OAuth-issued JWT token from <a href="https://go.tallyfy.com/">go.tallyfy.com</a>.
    Initialize a session and list tools:</p>

    <pre><code>JWT="&lt;your token&gt;"
ORG="&lt;your org id&gt;"

curl -X POST https://mcp.tallyfy.com/ \\
  -H "Authorization: Bearer $JWT" \\
  -H "X-Tallyfy-Org-Id: $ORG" \\
  -H "Content-Type: application/json" \\
  -H "Accept: application/json, text/event-stream" \\
  -H "MCP-Protocol-Version: 2025-06-18" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18",
                 "capabilities":{},
                 "clientInfo":{"name":"my-client","version":"1.0"}}}'</code></pre>

    <h2>Authentication</h2>
    <p>OAuth 2.1 with Dynamic Client Registration (DCR), PKCE S256, and RS256-signed JWTs.
    Discovery metadata is exposed at:</p>
    <ul>
        <li><a href="/.well-known/oauth-protected-resource"><code>/.well-known/oauth-protected-resource</code></a></li>
        <li><a href="/.well-known/oauth-authorization-server"><code>/.well-known/oauth-authorization-server</code></a></li>
        <li><a href="/.well-known/jwks.json"><code>/.well-known/jwks.json</code></a></li>
    </ul>

    <h2>Status</h2>
    <p>
        Server health: <a href="/health"><code>/health</code></a> &middot;
        Privacy: <a href="/privacy"><code>/privacy</code></a> &middot;
        Documentation: <a href="https://tallyfy.com/products/pro/integrations/mcp-server/">tallyfy.com/products/pro/integrations/mcp-server</a>
    </p>

    <div class="footer-links">
        <a href="https://tallyfy.com/products/pro/integrations/mcp-server/">Product docs</a>
        <a href="https://tallyfy.com/legal/privacy-policy/">Privacy policy</a>
        <a href="https://tallyfy.com/legal/">Terms</a>
        <a href="mailto:support@tallyfy.com">Support</a>
        <a href="https://tallyfy.com/">tallyfy.com</a>
    </div>

</div>

</body>
</html>
"""
