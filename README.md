# Tallyfy MCP Server

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
[![MCP](https://img.shields.io/badge/Model_Context_Protocol-server-5A45FF.svg)](https://modelcontextprotocol.io)

The official [Model Context Protocol](https://modelcontextprotocol.io) server for **[Tallyfy](https://tallyfy.com)**, the workflow and process automation platform. It lets any MCP-capable AI assistant run your operations: create and track tasks, launch and advance processes, read and edit templates, fill form fields, manage people and groups, and search across your organization, all through your own authenticated Tallyfy account.

> **This repository is an automatically published, read-only mirror.** The canonical source is maintained privately by Tallyfy and a sanitized, server-only snapshot is published here on every production release. Please do not open pull requests against this mirror (they cannot be merged here). For bugs or feature requests, use the issue tracker or contact us (see [Support](#support)). Hosting, deployment, and monitoring code is intentionally not part of this mirror.

## Connect (hosted, recommended)

Tallyfy runs a managed remote server. You do not need to host anything. Point your MCP client at:

```
https://mcp.tallyfy.com/
```

Transport is streamable HTTP and authentication is OAuth against your Tallyfy account, so the assistant only ever sees data the signed-in user is allowed to see.

<details>
<summary>Example client config</summary>

```json
{
  "mcpServers": {
    "tallyfy": {
      "type": "streamable-http",
      "url": "https://mcp.tallyfy.com/"
    }
  }
}
```
</details>

## What it can do

108 tools across the Tallyfy domain, grouped by area:

| Area | Examples |
|------|----------|
| Tasks | create, complete, reassign, comment on, and search tasks |
| Processes (runs) | launch a process from a template, advance and track steps |
| Templates (checklists) | read, create, and edit templates and their steps |
| Form fields | read and populate kick-off and step form fields |
| Automation | inspect and manage automated actions / rules |
| People & access | users, groups, guests, organization membership |
| Organization | tags, folders, comments, search across the org |

Every tool calls the public Tallyfy API on behalf of the authenticated user. There are no write operations the signed-in user could not perform in the Tallyfy app directly.

## Run it yourself

You can build and run the server from this mirror with Docker:

```bash
cd server
cp ../.env.example .env      # then fill in the values you need
docker build -t tallyfy-mcp-server .
docker run --rm -p 9000:9000 --env-file .env tallyfy-mcp-server
```

The server listens on port `9000` and speaks streamable HTTP at `/`. See [`.env.example`](./.env.example) for configuration. Python 3.11 is required if you prefer to run it without Docker (`pip install -r server/requirements.txt`, then `uvicorn server:app` from inside `server/`).

## Listed on

- [Official MCP Registry](https://registry.modelcontextprotocol.io) (`com.tallyfy/mcp-server`)
- [Smithery](https://smithery.ai/server/@tallyfy-inc/mcp-server)
- [Glama](https://glama.ai/mcp/connectors/com.tallyfy/mcp-server)
- [PulseMCP](https://www.pulsemcp.com/servers/tallyfy)

## Security

Tools are scoped to the authenticated user, inputs are validated and the outbound API surface is allowlisted. We disclose no static credentials in this repository: all secrets are supplied at runtime via environment variables. If you find a security issue, please email **security@tallyfy.com** rather than opening a public issue.

## Support

- Product docs: https://tallyfy.com/products/pro/integrations/mcp-server/
- Help / contact: https://tallyfy.com/contact/

## License

[Apache License 2.0](./LICENSE) © Tallyfy, Inc.
