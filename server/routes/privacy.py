"""
Privacy Policy Route

Serves the privacy policy page for the Tallyfy MCP server.
Required for public registry listings (Anthropic Claude Connectors,
OpenAI ChatGPT Apps, Google Gemini Enterprise, Smithery, MCP Registry).
"""

from starlette.responses import HTMLResponse


def register_privacy_routes(mcp):
    """Register privacy policy route with the MCP server."""

    @mcp.custom_route("/privacy", methods=["GET"])
    async def privacy_policy(request):
        """Serve the privacy policy page."""
        return HTMLResponse(PRIVACY_HTML)


PRIVACY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Privacy Policy &mdash; Tallyfy MCP Server</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.7;
            color: #1a1a1a;
            background: #fff;
            max-width: 720px;
            margin: 0 auto;
            padding: 48px 24px;
        }
        h1 { font-size: 28px; margin-bottom: 8px; }
        h2 { font-size: 20px; margin-top: 36px; margin-bottom: 12px; }
        p, li { font-size: 15px; margin-bottom: 12px; }
        ul { padding-left: 24px; margin-bottom: 12px; }
        .updated { color: #666; font-size: 14px; margin-bottom: 36px; }
        a { color: #0066cc; }
        table { width: 100%; border-collapse: collapse; margin: 16px 0; }
        th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #e5e5e5; font-size: 14px; }
        th { font-weight: 600; background: #f9f9f9; }
        .contact { background: #f5f5f5; padding: 20px; border-radius: 8px; margin-top: 24px; }
    </style>
</head>
<body>

<h1>Privacy Policy</h1>
<p class="updated">Tallyfy MCP Server for AI assistants &mdash; Last updated: May 2026</p>

<p>This privacy policy explains how the Tallyfy MCP Server ("the Service") collects, uses, and protects your data when accessed through any AI assistant or MCP-compatible client &mdash; including Anthropic Claude (desktop and claude.ai), OpenAI ChatGPT, Google Gemini (including Gemini Enterprise), Microsoft Copilot Studio, Cursor, and other tools that speak the Model Context Protocol.</p>

<h2>1. Who We Are</h2>
<p>The Service is operated by <strong>Tallyfy, Inc.</strong> ("Tallyfy", "we", "us"). The Service provides workflow automation tools through the Model Context Protocol (MCP), enabling you to manage tasks, processes, templates, and team members in your Tallyfy organization via your chosen AI assistant.</p>

<p>The MCP endpoint is published at <code>https://mcp.tallyfy.com/</code> (primary) and <code>https://mcp-gcp.tallyfy.com/</code> (Google Cloud Run mirror, used for Google Gemini Enterprise compatibility). Both endpoints route to the same Tallyfy backend with the same authentication and data-handling rules described below.</p>

<h2>2. Data We Collect</h2>
<p>We collect only the minimum data necessary to fulfill your requests:</p>

<table>
    <thead>
        <tr><th>Category</th><th>Data</th><th>Purpose</th></tr>
    </thead>
    <tbody>
        <tr>
            <td>Authentication</td>
            <td>OAuth 2.1 token (JWT)</td>
            <td>Verify your identity and authorize access to your Tallyfy organization</td>
        </tr>
        <tr>
            <td>Organization context</td>
            <td>Organization ID</td>
            <td>Scope requests to your organization's data</td>
        </tr>
        <tr>
            <td>Tool inputs</td>
            <td>Parameters you provide per request (e.g., search queries, task descriptions, email addresses for invitations)</td>
            <td>Execute the specific action you requested</td>
        </tr>
        <tr>
            <td>Server logs</td>
            <td>Request timestamps, tool names, error codes</td>
            <td>Operational monitoring and debugging</td>
        </tr>
        <tr>
            <td>AI session telemetry</td>
            <td>Per-turn metrics: model name, latency, token counts (input/output/cache), tool names invoked, status (ok/error), and short truncated excerpts (up to 200 characters) of your request text and the assistant's response &mdash; for operational monitoring only. Full conversation transcripts are <strong>not</strong> stored.</td>
            <td>Performance monitoring, error diagnosis, usage analytics, and capacity planning</td>
        </tr>
    </tbody>
</table>

<h2>3. Data We Do NOT Collect</h2>
<ul>
    <li>We do <strong>not</strong> store your JWT tokens or credentials on disk &mdash; they are held in memory only for the duration of your session</li>
    <li>We do <strong>not</strong> collect or store full AI conversation transcripts &mdash; only short truncated excerpts (max 200 characters per turn) for operational monitoring as described above</li>
    <li>We do <strong>not</strong> collect payment card information, health data, government identifiers, or passwords</li>
    <li>We do <strong>not</strong> collect precise geolocation data</li>
    <li>We do <strong>not</strong> build behavioral profiles or track you across sessions</li>
    <li>We do <strong>not</strong> use your data to train or fine-tune AI models &mdash; ours or any third party's</li>
</ul>

<h2>4. How We Use Your Data</h2>
<p>Your data is used exclusively to:</p>
<ul>
    <li><strong>Execute your requests</strong> &mdash; Each tool call is forwarded to the Tallyfy API on your behalf using your authenticated session</li>
    <li><strong>Return results</strong> &mdash; Responses from Tallyfy are passed back to your connected AI assistant for display; we do not modify, enrich, or aggregate them</li>
    <li><strong>Maintain service reliability</strong> &mdash; Server logs (without tokens or personal data) are used for error monitoring and performance</li>
</ul>

<h2>5. Data Sharing</h2>
<p>We share data only with the following recipients, strictly as needed to provide the service:</p>
<ul>
    <li><strong>Tallyfy API</strong> (go.tallyfy.com) &mdash; Your authenticated requests are forwarded to Tallyfy's API to read/write your organization data</li>
    <li><strong>Your chosen AI assistant provider</strong> &mdash; Tool results are returned to the AI assistant as part of the MCP protocol. Depending on the assistant you connect, this may be Anthropic (Claude), OpenAI (ChatGPT), Google (Gemini), Microsoft (Copilot Studio), or another MCP client. Each provider's own privacy policy governs how they handle this data on their side. We do not transmit your data to any AI provider you have not explicitly connected.</li>
    <li><strong>Tallyfy infrastructure</strong> &mdash; The MCP server runs on DigitalOcean (primary, US-West region) with a mirror on Google Cloud Run (used only for Gemini Enterprise connections). Tool call logs and AI session telemetry are stored on Tallyfy-operated servers, never on third-party AI providers.</li>
</ul>
<p>We do <strong>not</strong> sell, rent, or share your data with advertisers, data brokers, or any other third parties.</p>

<h2>6. Data Retention</h2>
<ul>
    <li><strong>Authentication tokens</strong>: Held in memory only; discarded when your session ends or times out (60 minutes)</li>
    <li><strong>Server logs</strong>: Retained for up to 30 days for operational purposes, then automatically deleted</li>
    <li><strong>AI session telemetry</strong>: Retained in our self-hosted analytics database with TimescaleDB compression for performance monitoring. Excerpts are truncated to 200 characters at capture time and are accessible only to Tallyfy operations personnel.</li>
    <li><strong>Tool request/response data</strong>: The full request/response payload is not persisted by the MCP Server; only the short telemetry excerpt above is captured. Processed transiently per request.</li>
</ul>

<h2>7. Security</h2>
<ul>
    <li>All connections use HTTPS/TLS encryption in transit</li>
    <li>JWT tokens are validated using RS256 cryptographic signatures</li>
    <li>Credentials are never written to disk or logged</li>
    <li>Per-session isolation ensures no data leakage between users</li>
    <li>OAuth 2.1 with PKCE for secure authentication flows</li>
</ul>

<h2>8. Your Rights and Controls</h2>
<p>You have the following controls over your data:</p>
<ul>
    <li><strong>Disconnect at any time</strong> &mdash; Remove the Tallyfy MCP server from your AI assistant's connector / settings to revoke access immediately (for example, Claude Desktop's <em>Connectors</em> panel, ChatGPT's <em>Apps &amp; GPTs</em>, or your client's equivalent)</li>
    <li><strong>Session control</strong> &mdash; Your session expires automatically after 60 minutes of inactivity</li>
    <li><strong>Data access and deletion</strong> &mdash; Contact us to request access to or deletion of any data we hold about you</li>
    <li><strong>Tallyfy account controls</strong> &mdash; Your underlying Tallyfy data is governed by <a href="https://tallyfy.com/privacy">Tallyfy's privacy policy</a></li>
</ul>

<h2>9. Children's Privacy</h2>
<p>The Service is not directed at children under 13. We do not knowingly collect personal information from children under 13. If you believe we have inadvertently collected such data, please contact us and we will delete it promptly.</p>

<h2>10. Changes to This Policy</h2>
<p>We may update this policy from time to time. Material changes will be reflected in the "Last updated" date above. Continued use of the Service after changes constitutes acceptance of the updated policy.</p>

<div class="contact">
    <h2 style="margin-top: 0;">11. Contact Us</h2>
    <p>If you have questions about this privacy policy or wish to exercise your data rights:</p>
    <ul>
        <li><strong>Email</strong>: <a href="mailto:privacy@tallyfy.com">privacy@tallyfy.com</a></li>
        <li><strong>Website</strong>: <a href="https://tallyfy.com">tallyfy.com</a></li>
    </ul>
</div>

</body>
</html>
"""
