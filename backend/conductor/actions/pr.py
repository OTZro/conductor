from __future__ import annotations

from ..config import settings
from ..proc import run

_MERGE_FLAG = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}


async def approve(repo: str, number: int, body: str | None = None) -> dict:
    args = ["pr", "review", str(number), "-R", repo, "--approve"]
    if body:
        args += ["--body", body]
    await run(settings.gh_bin, *args)
    return {"ok": True}


async def merge(repo: str, number: int, method: str = "squash") -> dict:
    flag = _MERGE_FLAG.get(method, "--squash")
    out = await run(settings.gh_bin, "pr", "merge", str(number), "-R", repo, flag)
    return {"ok": True, "detail": out.strip()[:300]}
