"""Host metrics for the Monitor tab — CPU / memory / swap / disk / battery /
uptime / session counts, for the local machine and every configured remote.

One compound shell script per host (a single ssh round-trip for remotes, via the
same runner terminal.py uses), parsed here. macOS-flavored commands — both base
and roam are Macs; a non-Mac host would just report zeros for what's missing.
"""

from __future__ import annotations

import asyncio
import re
import time

from conductor.config import settings
from conductor.actions import terminal as term

# sections separated by --- markers, in this exact order. `[c]laude` so the pgrep
# doesn't match this script's own command line.
#
# PATH is set explicitly: the LOCAL host runs this via the backend daemon's own
# env, which (depending on how it was launched — nohup/launchd) may lack /usr/sbin
# (where `sysctl` lives) and /opt/homebrew/bin (where `tmux` lives), silently
# zeroing CPU/mem/load/swap/sessions. Remote runs in a login shell that already
# has these, so setting it again is harmless. Don't trust the inherited PATH.
_SCRIPT = r"""export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/sbin:/usr/bin:/bin:/sbin:$PATH"
sysctl -n hw.ncpu hw.memsize 2>/dev/null
sysctl -n vm.loadavg 2>/dev/null
sysctl -n kern.boottime 2>/dev/null
echo ---
vm_stat 2>/dev/null
echo ---
df -k /System/Volumes/Data 2>/dev/null || df -k / 2>/dev/null
echo ---
pmset -g batt 2>/dev/null
echo ---
tmux ls -F '#{session_name}' 2>/dev/null
echo ---
sysctl -n vm.swapusage 2>/dev/null
echo ---
pgrep -f 'local/bin/[c]laude' 2>/dev/null | wc -l"""


def _f(x: str) -> float:
    try:
        return float(x)
    except ValueError:
        return 0.0


def _size_mb(tok: str) -> float:
    """'2048.00M' / '3.50G' → MB."""
    m = re.match(r"([\d.]+)([MG])", tok)
    if not m:
        return 0.0
    v = float(m.group(1))
    return v * 1024 if m.group(2) == "G" else v


def parse_metrics(raw: str, now: float | None = None) -> dict:
    """Pure parser for _SCRIPT's output (unit-testable, no subprocess)."""
    now = time.time() if now is None else now
    parts = re.split(r"^---$", raw, flags=re.M)
    while len(parts) < 7:
        parts.append("")
    sys_s, vm_s, df_s, batt_s, tmux_s, swap_s, claude_s = (p.strip() for p in parts[:7])

    # sysctl: line1 ncpu, line2 memsize, line3 "{ 1.2 3.4 5.6 }", line4 "{ sec = N, ... }"
    lines = [ln.strip() for ln in sys_s.splitlines() if ln.strip()]
    cores = int(_f(lines[0])) if lines else 0
    mem_total = int(_f(lines[1])) if len(lines) > 1 else 0
    load = [0.0, 0.0, 0.0]
    m = re.search(r"\{\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\}", sys_s)
    if m:
        load = [float(m.group(i)) for i in (1, 2, 3)]
    boot = re.search(r"sec\s*=\s*(\d+)", sys_s)
    uptime_s = int(now - int(boot.group(1))) if boot else 0

    # vm_stat → used ≈ (active + wired + compressor-occupied) * page_size
    page = 4096
    m = re.search(r"page size of (\d+)", vm_s)
    if m:
        page = int(m.group(1))

    def pages(label: str) -> int:
        mm = re.search(rf"{label}:\s+([\d]+)", vm_s)
        return int(mm.group(1)) if mm else 0

    mem_used = (
        pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")
    ) * page

    # df -k: header + one data line (1024-blocks, Used, ... Mounted on)
    disk = {"total": 0, "used": 0, "pct": 0.0, "mount": ""}
    dlines = [ln for ln in df_s.splitlines() if ln.strip()]
    if len(dlines) >= 2:
        f = dlines[-1].split()
        if len(f) >= 3:
            total_b, used_b = int(_f(f[1])) * 1024, int(_f(f[2])) * 1024
            disk = {
                "total": total_b,
                "used": used_b,
                "pct": round(used_b / total_b * 100, 1) if total_b else 0.0,
                "mount": f[-1],
            }

    battery = None
    m = re.search(r"(\d+)%;\s*([\w ]+?);", batt_s)
    if m:
        battery = {
            "pct": int(m.group(1)),
            "state": m.group(2).strip(),
            "ac": "AC Power" in batt_s,
        }

    names = [ln.strip() for ln in tmux_s.splitlines() if ln.strip()]
    sessions = {
        "conductor": sum(1 for n in names if n.startswith("conductor-")),
        "tmux_other": sum(1 for n in names if not n.startswith("conductor-")),
        "claude": int(_f(claude_s.strip() or "0")),
    }

    swap = {"total_mb": 0.0, "used_mb": 0.0}
    m = re.search(r"total\s*=\s*(\S+)\s+used\s*=\s*(\S+)", swap_s)
    if m:
        swap = {"total_mb": _size_mb(m.group(1)), "used_mb": _size_mb(m.group(2))}

    return {
        "cores": cores,
        "load": load,
        "cpu_pct": round(load[0] / cores * 100, 1) if cores else 0.0,
        "mem": {
            "total": mem_total,
            "used": mem_used,
            "pct": round(mem_used / mem_total * 100, 1) if mem_total else 0.0,
        },
        "swap": swap,
        "disk": disk,
        "uptime_s": uptime_s,
        "battery": battery,
        "sessions": sessions,
    }


async def host_metrics(host: str | None) -> dict:
    """Metrics for one host; {online: False} when unreachable/failed."""
    if host and not await term.reachable(host):
        return {"host": host, "online": False}
    rc, out = await term._run(["sh", "-c", _SCRIPT], host=host, timeout=12)
    text = out.decode(errors="replace") if out else ""
    if rc != 0 and not text.strip():
        return {"host": host, "online": False}
    d = parse_metrics(text)
    d.update({"host": host, "online": True})
    return d


_CACHE: dict = {"ts": 0.0, "data": None}


async def all_metrics() -> list[dict]:
    """Every host's metrics, concurrently, cached briefly (several dashboard
    viewers shouldn't multiply the ssh probes)."""
    now = time.monotonic()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < 5:
        return _CACHE["data"]
    hosts: list[str | None] = [None, *settings.remote_host_map]
    data = list(await asyncio.gather(*(host_metrics(h) for h in hosts)))
    _CACHE["ts"], _CACHE["data"] = now, data
    return data
