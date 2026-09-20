#!/usr/bin/env python3
"""Assert the Bauer overlay merged to the intended shape.

`docker compose config` on its own only proves the files parse. The failure this
overlay hit was semantic, not syntactic: Compose MERGES list fields across files
rather than replacing them, so the UI's `ports` override appended to upstream's
and the 0.0.0.0:3000 binding survived alongside the intended one. The override
looked correct and the UI still answered on every interface.

Each check below pins one property of the rendered result. Run it against the
rendered output:

    docker compose -f docker/docker-compose.yml \\
                   -f bauer/docker-compose.bauer.yml config > merged.yml
    python3 .github/scripts/assert_bauer_overlay.py merged.yml
"""

from __future__ import annotations

import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it explicitly
    sys.exit("PyYAML is required: pip install pyyaml")

FAILURES: list[str] = []
CHECKS = 0

LOOPBACK = {"127.0.0.1", "::1"}


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")
        FAILURES.append(label)


def duration_seconds(value: object) -> float | None:
    """Parse a Compose duration ('300s', '5m0s', '1h2m3s') into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", value):
        matched = True
        total += float(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total if matched else None


def main(path: str) -> int:
    with open(path) as handle:
        merged = yaml.safe_load(handle)

    services = merged.get("services") or {}
    print(f"services rendered: {', '.join(sorted(services)) or '(none)'}\n")

    # 1. The UI publishes no host port; `expose` only.
    ui = services.get("ui") or {}
    check(
        "ui publishes no host port",
        not ui.get("ports"),
        f"got {ui.get('ports')!r} — did `ports: !reset []` survive the merge?",
    )
    check("ui exposes 3000 on the compose network", "3000" in [str(p) for p in ui.get("expose") or []])

    # 2. Published ports bind loopback only.
    for name, service in sorted(services.items()):
        for port in service.get("ports") or []:
            host_ip = port.get("host_ip") if isinstance(port, dict) else None
            check(
                f"{name} publishes {port.get('published') if isinstance(port, dict) else port} on loopback only",
                host_ip in LOOPBACK,
                f"host_ip={host_ip!r}",
            )

    # 3. Exactly one API replica. The scheduler claims rows with
    #    UPDATE...RETURNING, which is safe within a process but not across them,
    #    and there is no leader election: a second replica double-fires every
    #    scheduled action.
    api = services.get("api") or {}
    check("api is pinned to exactly one replica", (api.get("deploy") or {}).get("replicas") == 1,
          f"got {(api.get('deploy') or {}).get('replicas')!r}")

    # 4. Alert review annotates only.
    env = api.get("environment") or {}
    if isinstance(env, list):  # compose can render either shape
        env = dict(item.split("=", 1) for item in env if "=" in item)
    check("ALERT_REVIEW_MAX_MOVES_PER_SCAN is 0",
          str(env.get("ALERT_REVIEW_MAX_MOVES_PER_SCAN")) == "0",
          f"got {env.get('ALERT_REVIEW_MAX_MOVES_PER_SCAN')!r}")

    # 5. Cold start builds the MCP tool-discovery index and loads Chroma before
    #    serving. A short grace kills the container mid-boot, in a crash loop
    #    that reads like a deploy failure.
    grace = duration_seconds((api.get("healthcheck") or {}).get("start_period"))
    check("api health-check grace is at least 300s", grace is not None and grace >= 300,
          f"got {(api.get('healthcheck') or {}).get('start_period')!r}")

    print()
    if FAILURES:
        print(f"bauer-overlay: FAIL — {len(FAILURES)} of {CHECKS} checks failed")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print(f"bauer-overlay: OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    raise SystemExit(main(sys.argv[1]))
