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

## Validation

`.github/workflows/bauer-overlay.yml` renders the merged compose file and runs
`.github/scripts/assert_bauer_overlay.py` over it on any change to `bauer/` or
`docker/`. Six assertions, each one a claim this README makes: the UI publishes
no host port, nothing binds a non-loopback interface, the API is pinned to one
replica, the propose-only posture is set, and the health-check grace is at least
300s. Verified to fail (exit 1) when `!reset []` is removed from the UI's port
override, which is the regression it exists for.

Actions is enabled on this fork, so the gate runs. It first ran green on
`bauer-automate/3P-OpenExecutive#2`. That resolves the "nothing validates
`bauer/`" gap: upstream's `ci.yml` does not look at this directory and is not
edited to, so this workflow is the only thing that does.

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

## How the `m365-graph` surface is narrowed

The engine runs with `--org-mode --read-only --enabled-tools '^(?!.*sharepoint)'`.
Three deliberate choices, all verified against `3P-ms-365-mcp-server@70a54bc`
rather than taken from the README.

**`--read-only` is a tool-surface filter, not a runtime guard.** Tools it drops
never appear in `tools/list` at all. It removes every non-GET endpoint, keeping
7 POST-shaped reads and the 4 utility tools. That is the belt under the
read-scopes-first posture in `claude-skills#933`; the service account's scopes
are still the actual boundary.

It costs `graph-batch`, which is a POST carrying no `readOnly` flag. No flag
keeps batching while dropping the other writes. Accepted here, because the
engine does not need it; an interactive consumer that does runs without
`--read-only`.

**SharePoint is excluded by regex, because no preset can do it.** SharePoint
routes to `bauer-sharepoint`'s own MCP server (operator ruling 2026-09-19).
There is no `sharepoint` preset to switch off: those 34 tools sit inside the
`work` preset alongside Teams. `--enabled-tools` compiles as
`new RegExp(pattern, 'i')` against `toolName`, so negative lookahead works.

Surface sizes, counted from `src/endpoints.json` plus the 4 utility tools:

| Flags | Endpoint tools | With utilities |
|---|---|---|
| none | 332 | 336 |
| `--read-only` | 162 | 166 |
| `--enabled-tools '^(?!.*sharepoint)'` | 298 | 302 |
| both, as configured here | 141 | 145 |

**Before write scopes are ever granted**, set `--message-signoff-prefix` (or
`MS365_MCP_MESSAGE_SIGNOFF_PREFIX`) so recipients can tell an agent sent the
message. The server refuses to start with a marker that renders as empty text.

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
- Pin the `@softeria/ms-365-mcp-server` version. `PIN_ME` is deliberate, and it
  **cannot be resolved by reading the fork**: its `package.json` says
  `"version": "0.0.0-development"` because semantic-release stamps the real
  version at publish time. Use `npm view @softeria/ms-365-mcp-server versions`.
- `claude-skills#886`'s customer-data ruling still gates anything beyond Bauer's
  own content. Bauer SOPs are cleared; customer SOWs and Zoho customer records
  are not.
