# `bauer/` — Bauer Automate deployment overlay

Everything here is **additive**. No file upstream owns is edited, because upstream
is active (PR #126 at the time of writing) and every in-place edit is a merge cost
we pay forever. Decisions: `bauer-automate/claude-skills` #886, #897, #903, #933.

## Files

| File | Purpose |
|---|---|
| `mcp_servers.json` | Template for `/data/company/mcp_servers.json`. Placeholders only. |
| `env.bauer.example` | Values to merge into the gitignored repo-root `.env`. |
| `docker-compose.bauer.yml` | Overlay for the split topology. |

## Running it

```bash
docker compose -f docker/docker-compose.yml -f bauer/docker-compose.bauer.yml up -d
```

Validate a change before applying it:

```bash
docker compose -f docker/docker-compose.yml -f bauer/docker-compose.bauer.yml config
```

That command is not optional. Compose **merges** list fields across files rather
than replacing them, which already bit this overlay once: the UI's port list
merged, so upstream's `0.0.0.0:3000` binding survived alongside the intended one
and the UI still answered on every interface. `!reset []` fixes it, and `config`
is how you see that it worked. `!reset` needs Compose v2.24 or newer.

## What the overlay changes, and why

1. **UI runs the production image** (`docker/Dockerfile.ui`) instead of upstream's
   dev container, which runs `npm install && npm run dev` on boot.
2. **UI publishes no host port** — only `expose: 3000` on the compose network, so
   the edge connector reaches `ui:3000` and the host opens nothing.
3. **API pinned to one replica.** The scheduler claims rows with
   `UPDATE … RETURNING`, safe within a process but not across them. A second
   replica fires every scheduled action twice. There is no leader election.
4. **Health-check grace of 5 minutes.** Cold start builds the MCP tool-discovery
   index and loads Chroma before serving; a short grace kills it mid-boot.
5. **`ALERT_REVIEW_MAX_MOVES_PER_SCAN=0`** — propose-only posture.

## Topology

Box holds the data, a cloud edge fronts it, and **the link is outbound-initiated
from the box**. No inbound port to the operator's network, ever. The edge
terminates TLS, holds the public DNS name, and holds no data.

The edge service is left commented out in the overlay rather than guessed.
Cloudflare Tunnel is the obvious fit, but that account was never inspected.

## Secrets

The gateway forwards only the variables named in
`orchestrator/mcp_gateway.py::_FORWARDED_ENV_VARS` to the `extensible-mcp` child.
`MS365_*`, `ZOHO_*` and `GITHUB_*` are **not** on that list, so `$VAR`
interpolation in `mcp_servers.json` will not resolve for them.

Put literal values in the copy on `/data/company/mcp_servers.json`, which is on
the volume and never committed. Do **not** extend `_FORWARDED_ENV_VARS` — that is
an upstream file, and the merge cost outlives the convenience.

## Open before first run

- `zoho` and `github` entries in `mcp_servers.json` are marked
  `CONFIRM_BEFORE_USE`. Zoho's MCP is a hosted remote endpoint and it is not
  known whether `extensible-mcp` accepts remote URL servers or stdio only — that
  package is external and was not inspected. Confirm rather than guess.
- Pin the `@softeria/ms-365-mcp-server` version. `PIN_ME` is deliberate.
- `claude-skills#886`'s customer-data ruling still gates anything beyond Bauer's
  own content. Bauer SOPs are cleared; customer SOWs and Zoho customer records
  are not.
