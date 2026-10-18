# `bauer/` — Bauer Automate deployment overlay

Everything here is **additive**. No file upstream owns is edited, because upstream
is active and every in-place edit is a merge cost we pay forever.

> **Design rationale, topology and data-handling rules are recorded in Bauer's
> internal tracker, not here.** This repository is public. Keep it to mechanics:
> what the files do and how to run them. Anything explaining *why* a control
> exists, what it protects, or what the deployment holds belongs in the internal
> record.

## Files

| File | Purpose |
|---|---|
| `mcp_servers.json` | Template for `/data/company/mcp_servers.json`. No secrets. |
| `env.bauer.example` | Values to merge into the gitignored repo-root `.env`. |
| `docker-compose.bauer.yml` | Compose overlay. |

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

## Validation

`.github/workflows/bauer-overlay.yml` renders the merged compose file and runs
`.github/scripts/assert_bauer_overlay.py` over it on any change to `bauer/` or
`docker/`. Six assertions over the rendered result. Verified to fail (exit 1)
when `!reset []` is removed from the UI's port override, which is the regression
it exists for.

Note the `paths:` filter. An upstream sync that touches neither `bauer/` nor
`docker/` will not run the gate, which is correct but means a green PR is not by
itself evidence the overlay was re-checked. Run it by hand after a sync.

Upstream's `ci.yml` does not look at this directory and is not edited to, so this
workflow is the only thing that does.

## What the overlay changes

1. **UI runs the production image** (`docker/Dockerfile.ui`) instead of upstream's
   dev container, which runs `npm install && npm run dev` on boot.
2. **UI publishes no host port** — `expose: 3000` on the compose network only.
3. **API pinned to one replica.** The scheduler claims rows with
   `UPDATE … RETURNING`, safe within a process but not across them. A second
   replica fires every scheduled action twice. There is no leader election.
4. **Health-check grace of 5 minutes.** Cold start builds the MCP tool-discovery
   index and loads Chroma before serving; a short grace kills it mid-boot.
5. **`ALERT_REVIEW_MAX_MOVES_PER_SCAN=0`**.

The edge service is left commented out rather than guessed.

## Microsoft 365 (`microsoft_365`)

Uses upstream's co-located server: `@softeria/ms-365-mcp-server@0.154.2`, baked
into the API image and started by `docker/ms365-mcp-launch.sh`. Nothing here
installs or pins it; upstream's `docker/Dockerfile` does.

The entry adds two flags and one tool list to upstream's block:

| Setting | Effect |
|---|---|
| `--read-only` | Drops every non-GET tool before registration. Also narrows the scopes `--login` asks for. |
| `--org-mode` | Turns on the Teams tools. |
| `MS365_MCP_ENABLED_TOOLS` | Mail, calendar and Teams **reads** only. Replaces the launcher's default list, which includes send and calendar writes. |

Verified against 0.154.2 through the real launcher: 31 tools register (24
reads, `download-bytes`, 6 account tools) and `--list-permissions` asks for
`Mail.Read`, `Calendars.Read`, `Chat.Read`, `ChatMember.Read`,
`ChatMessage.Read`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`,
`ChannelMessage.Read.All` and `TeamMember.Read.All`. No OneDrive, no SharePoint.

Keep the server name `microsoft_365`. The gateway's mail and calendar checks,
and its block on the `login`, `logout`, `select-account` and `remove-account`
tools, key on the `microsoft_365__` prefix. Any other name skips them.

The gateway has **no checks for Teams tools**. That is safe here only because
every Teams tool in the list is a read. Adding a Teams write (send, reply,
react) needs a matching check in `orchestrator/mcp_gateway.py` first.

Leave `EMAIL_PROVIDER` and `CALENDAR_PROVIDER` unset while read-only. Setting
either to `microsoft` makes the inbox poller and the booking tools call
Outlook write tools that this list does not register.

### Sign in

`--login` must run with the same flags and tool list as the gateway, or it
asks for the wrong scopes. This reads the list from the config on the volume:

```bash
docker compose exec api sh -c 'MS365_MCP_ENABLED_TOOLS="$(python -c "import json; print(json.load(open(\"/data/company/mcp_servers.json\"))[\"mcpServers\"][\"microsoft_365\"][\"env\"][\"MS365_MCP_ENABLED_TOOLS\"])")" exec /usr/local/bin/ms365-mcp-launch.sh --read-only --org-mode "$0"' --list-permissions
```

1. Run the command above with `--list-permissions`. Check the scopes match the list above.
2. Grant those delegated scopes, plus `User.Read` and `offline_access`, with admin consent.
3. Run the same command with `--login`. Sign in as the service account.
4. Run it with `--verify-login`.
5. Restart the API: `docker compose restart api`.

The app registration needs *Allow public client flows: Yes* (device-code
sign-in). Run `--login` again whenever the tool list changes.

## Runtime server loading is off

`filters.load_control.deny_url_patterns` is `["*"]`, so the model cannot load an
MCP server by URL at runtime (`load_mcp_server`). Upstream's `["http://*"]`
alone does not do this: `extensible-mcp` allows every URL the deny list misses
when `allow_url_patterns` is empty, so any `https://` server could load. The
servers listed in `mcpServers` are not affected.

## Gateway environment variables

Upstream's gateway forwards the `MS365_MCP_*` names the `microsoft_365` entry
uses (`orchestrator/mcp_gateway.py::_FORWARDED_ENV_VARS`), so its `$VAR`
values resolve from the API's environment. `ZOHO_*` and `GITHUB_*` are **not**
forwarded: put literal values for those in the copy on
`/data/company/mcp_servers.json`. Do **not** extend `_FORWARDED_ENV_VARS`. It
is an upstream file.

## Open before first run

- `zoho` and `github` are placeholders (`CONFIRM_BEFORE_USE`). `extensible-mcp`
  accepts a remote server as `{"url": "https://..."}`, but it cannot send auth
  headers. Zoho works that way only if its endpoint carries the key in the URL.
  GitHub's remote server needs a bearer header, so it needs a local (stdio)
  server in the image instead.
- Content-ingest scope is governed by the internal tracker. Check it before
  loading anything into the corpus.
