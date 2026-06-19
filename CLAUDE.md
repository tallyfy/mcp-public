# CLAUDE.md — tallyfy/mcp-public

## This repository is an automatically generated, read-only mirror

The code here is a **sanitized, server-only snapshot** of Tallyfy's MCP server. The canonical source is maintained in a private Tallyfy repository. On every production release, a GitHub Actions workflow assembles an allowlisted subset of the private repo, runs fail-closed secret/internal-pattern scans, and force-syncs the result here.

**Do not edit files in this repo directly.** Any change is overwritten on the next sync. There is no value in opening a pull request against this mirror; it cannot be merged upstream from here.

- To change the **server code**, edit it in the private source repo.
- To change these **public docs** (this file or `README.md`), edit `public-mirror/CLAUDE.public.md` / `public-mirror/README.public.md` in the private repo. They are overlaid into this mirror at publish time.

## What is and is not here

Published: the MCP server itself (`server/` — tools, routes, middleware, utils), `server.json`, `LICENSE`, `architecture.mmd`, this `README.md`/`CLAUDE.md`, and `.env.example`.

Deliberately not published (kept private): hosting/WebSocket layer, monitoring and alerting, deployment workflows, internal runbooks, and any infrastructure identifiers. This mirror is the **source you can read and run**, not the infrastructure that runs the managed endpoint.

## Using the server

- Hosted endpoint: `https://mcp.tallyfy.com/` (streamable HTTP, OAuth against a Tallyfy account).
- Run locally: see `README.md` (Docker, or Python 3.11 + `server/requirements.txt`).
- Tools: 108, scoped to the authenticated user; all call the public Tallyfy API.

## Reporting issues

Security issues: **security@tallyfy.com**. Product questions: https://tallyfy.com/contact/.
