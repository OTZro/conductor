"""OrbStack sandbox (VM) inventory for the Sandbox tab — every OrbStack machine on
the local Mac and each configured remote, with its disk footprint, state, and a
best-effort live probe (process count) for running ones.

Sandboxes here are OrbStack Linux VMs: per-ticket build VMs (``proj-*``), plus the
dev VM (``fms-dev`` / ``sandbox``). One ``orb list`` round-trip per host is
the inventory; a second bounded round-trip fans out ``orb run`` INSIDE each running
VM. Both ride the same runner terminal.py uses (one ssh per call for remotes).

Resource note: OrbStack VMs SHARE the Mac's RAM/CPU/disk — there is no per-VM cap —
so the meaningful *per-VM* figure is its disk-image size (what ``orb list`` reports on
newer orb; the older CLI on base omits it). Inside a VM, ``/proc/loadavg`` and uptime
read the shared OrbStack host, identical across that host's VMs, so we surface those
once per host and keep only the genuinely per-VM process count on each row. Host-wide
CPU/mem already live in the Monitor tab.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import os

from conductor.config import settings
from conductor.actions import terminal as term

# plugin-owned config (was config.sandbox_link_port) — read our own env, not core config
_LINK_PORT = int(os.environ.get("CONDUCTOR_SANDBOX_LINK_PORT", "8000"))

# orb lives in /opt/homebrew/bin or /usr/local/bin; the daemon's inherited PATH may
# lack it (same reasoning as metrics._SCRIPT), so set it explicitly.
_PATH = 'export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"'

# inventory: canonical JSON (stable fields across orb versions) + the text form (newer
# orb adds a per-VM disk column and the ip the JSON omits), in one round-trip.
_LIST_SCRIPT = f"""{_PATH}
orb list --format json 2>/dev/null
echo '===TEXT==='
orb list 2>/dev/null"""

# live probe: for each RUNNING machine, one `orb run` reads loadavg / uptime / process
# count / core count from its /proc. The whole fan-out is ONE remote shell (one ssh);
# each VM's block is prefixed @@@<name>. loadavg/uptime/cores come back identical per
# host (shared OrbStack host); only the process count is per-VM.
_DETAIL_SCRIPT = f"""{_PATH}
for m in $(orb list 2>/dev/null | awk '$2=="running"{{print $1}}'); do
  printf '@@@%s\\n' "$m"
  orb run -m "$m" sh -c 'cut -d" " -f1 /proc/loadavg; cut -d. -f1 /proc/uptime; ls -d /proc/[0-9]* 2>/dev/null | wc -l; nproc' 2>/dev/null
done"""


def _f(x: str) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_disk_ip(text: str) -> dict[str, dict]:
    """From ``orb list`` text rows → {name: {disk_gb, ip}}. The column layout varies by
    orb version (older base has no disk column), so detect the ``<N> GB`` disk token and
    the IPv4 positionally rather than by fixed index."""
    out: dict[str, dict] = {}
    ip_re = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
    for ln in text.splitlines():
        toks = ln.split()
        if len(toks) < 2 or toks[1] not in ("running", "stopped", "paused"):
            continue
        disk_gb = ip = None
        for i, t in enumerate(toks):
            if t == "GB" and i > 0:
                disk_gb = _f(toks[i - 1])
            elif ip_re.match(t):
                ip = t
        out[toks[0]] = {"disk_gb": disk_gb, "ip": ip}
    return out


def parse_sandboxes(list_raw: str, detail_raw: str = "", link_port: int = 8000) -> dict:
    """Pure parser (unit-testable, no subprocess). Merges orb JSON + text disk/ip + the
    per-VM process count, and lifts the host-shared load/uptime/cores out separately.
    Each running VM also gets its OrbStack URL http://<name>.orb.local:<link_port>/ ."""
    json_part, _, text_part = list_raw.partition("===TEXT===")
    try:
        machines = json.loads(json_part.strip() or "[]")
    except json.JSONDecodeError:
        machines = []
    disk_ip = _parse_disk_ip(text_part)

    procs: dict[str, int] = {}
    shared: dict = {"load1": None, "uptime_s": None, "cores": None}
    for block in detail_raw.split("@@@"):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 5:  # name + 4 numbers
            continue
        name, nums = lines[0], lines[1:]
        pc = _f(nums[2])
        if pc is not None:
            procs[name] = int(pc)
        if shared["load1"] is None:  # identical across a host's VMs → take the first
            shared = {"load1": _f(nums[0]), "uptime_s": int(_f(nums[1]) or 0), "cores": int(_f(nums[3]) or 0)}

    vms = []
    for m in machines:
        name = m.get("name") or ""
        img = m.get("image") or {}
        di = disk_ip.get(name, {})
        state = m.get("state") or "unknown"
        vms.append({
            "name": name,
            "state": state,
            "distro": img.get("distro"),
            "version": img.get("version"),
            "arch": img.get("arch"),
            "builtin": bool(m.get("builtin")),
            "disk_gb": di.get("disk_gb"),
            "ip": di.get("ip"),
            "procs": procs.get(name),
            # OrbStack serves the VM at <name>.orb.local; only a running one can answer
            "link": f"http://{name}.orb.local:{link_port}/" if state == "running" and name else None,
        })
    vms.sort(key=lambda v: (v["state"] != "running", v["name"]))
    return {"vms": vms, "shared": shared}


def _empty(host: str | None, online: bool) -> dict:
    return {
        "host": host, "online": online, "vms": [], "shared": {},
        "summary": {"total": 0, "running": 0, "stopped": 0, "disk_gb": 0.0},
    }


async def host_sandboxes(host: str | None) -> dict:
    """One host's OrbStack inventory; {online: False} when unreachable/orb-less."""
    if host and not await term.reachable(host):
        return _empty(host, False)
    rc, out = await term._run(["sh", "-c", _LIST_SCRIPT], host=host, timeout=12)
    text = out.decode(errors="replace") if out else ""
    if "===TEXT===" not in text:  # orb absent or host down → not a live inventory
        return _empty(host, False)

    detail_raw = ""
    try:  # best-effort: a hung VM must not sink the inventory (the timeout caps it)
        _, dout = await term._run(["sh", "-c", _DETAIL_SCRIPT], host=host, timeout=18)
        detail_raw = dout.decode(errors="replace") if dout else ""
    except Exception:  # noqa: BLE001
        detail_raw = ""

    parsed = parse_sandboxes(text, detail_raw, _LINK_PORT)
    vms = parsed["vms"]
    running = sum(1 for v in vms if v["state"] == "running")
    return {
        "host": host,
        "online": True,
        "vms": vms,
        "shared": parsed["shared"],
        "summary": {
            "total": len(vms),
            "running": running,
            "stopped": len(vms) - running,
            "disk_gb": round(sum(v["disk_gb"] or 0 for v in vms), 1),
        },
    }


log = logging.getLogger("conductor.sandbox")

_CACHE: dict = {"ts": 0.0, "data": None}
# A full scan ssh's to every remote host and runs `orb list` there — measured at 13.4s
# on this setup, which is why the window matters so much. FRESH is how long a scan is
# reused outright; past it the cached answer is still SERVED, and a refresh runs behind
# it (stale-while-revalidate). Only a cold cache waits.
#
# The old 5s window meant every card opened more than 5s after the last one paid the
# full 13.4s, and — because a card's widgets are fetched together over HTTP/1.1's
# six-connection budget — held a connection the terminal was waiting for.
_FRESH_S = 60.0
_refresh_task: asyncio.Task | None = None
_cold_lock = asyncio.Lock()


async def _scan() -> list[dict]:
    hosts: list[str | None] = [None, *settings.remote_host_map]
    data = list(await asyncio.gather(*(host_sandboxes(h) for h in hosts)))
    _CACHE["ts"], _CACHE["data"] = time.monotonic(), data
    return data


def _kick_refresh() -> None:
    """Refresh behind the caller, at most one scan in flight. Single-flight is the whole
    point: a board with several cards open would otherwise multiply the ssh probes the
    cache exists to collapse."""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return

    async def _run() -> None:
        try:
            await _scan()
        except Exception as exc:  # noqa: BLE001 — a failed refresh keeps the stale data
            log.warning("[sandbox] background inventory refresh failed: %s", exc)

    _refresh_task = asyncio.create_task(_run())


async def all_sandboxes() -> list[dict]:
    """Every host's inventory, concurrently, cached briefly so several viewers don't
    multiply the ssh probes (mirrors metrics.all_metrics)."""
    cached = _CACHE["data"]
    if cached is not None:
        if time.monotonic() - _CACHE["ts"] >= _FRESH_S:
            _kick_refresh()  # stale: answer now, correct behind
        return cached
    # cold: single-flight, like the background refresh — several cards opened right
    # after boot would otherwise each launch their own 13s ssh sweep
    async with _cold_lock:
        if _CACHE["data"] is not None:  # someone filled it while we queued
            return _CACHE["data"]
        return await _scan()
