from __future__ import annotations

import asyncio


async def run(bin: str, *args: str, timeout: float = 30) -> str:
    """Run a subprocess, return stdout (text). Raise RuntimeError with stderr on
    non-zero exit, or on timeout (so a hung acli/gh never wedges a caller).
    Used for acli/gh/tmux/ttyd shell-outs."""
    proc = await asyncio.create_subprocess_exec(
        bin,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError(f"{bin} {' '.join(args)} timed out after {timeout}s")
    if proc.returncode != 0:
        detail = (err or b"").decode(errors="replace")[:400]
        raise RuntimeError(f"{bin} {' '.join(args)} failed (rc={proc.returncode}): {detail}")
    return out.decode(errors="replace")
