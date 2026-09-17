from __future__ import annotations

import json

from fastapi import APIRouter

from ..config import settings

router = APIRouter(prefix="/api", tags=["termkeys"])


@router.get("/termkeys")
async def list_termkeys() -> list[dict]:
    """Mobile terminal soft-keys, read from an OPTIONAL local file
    (CONDUCTOR_TERMKEYS_FILE, default ~/.conductor/termkeys.json — OUTSIDE the repo).
    Absent/invalid/empty → ``[]``, and the frontend falls back to its built-in defaults
    (Esc/Tab/arrows/^C…/tmux ⌥-keys).

    Each entry is one button, either:
      - a synthetic keydown: {"label","key","code"?,"keyCode","ctrlKey"?,"altKey"?,…}
      - a raw byte sequence:  {"label","seq","title"?}   (tmux-style, e.g. "\\u001b[1;3D")

    Example ~/.conductor/termkeys.json:
      [{"label":"Esc","key":"Escape","keyCode":27},
       {"label":"⌥←","seq":"\\u001b[1;3D","title":"prev window"}]
    """
    try:
        data = json.loads(settings.termkeys_file.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for d in data:
        if not isinstance(d, dict) or not d.get("label"):
            continue
        # a button is valid if it can DO something: inject a sequence or fire a keydown
        if not d.get("seq") and d.get("keyCode") is None and not d.get("key"):
            continue
        out.append(d)
    return out


@router.get("/hotkeys")
async def get_hotkeys() -> dict[str, str]:
    """Desktop physical-keyboard remap, read from an OPTIONAL local file
    (CONDUCTOR_HOTKEYS_FILE, default ~/.conductor/hotkeys.json — OUTSIDE the repo).
    Absent/invalid/empty → ``{}``, and the frontend keeps its built-in DEFAULT_HOTKEYS.

    Flat map of ``"<mods>-<KeyboardEvent.code>" -> raw bytes to inject``, where mods is
    any of M(⌘) C(⌃) A(⌥) S(⇧) in that order. e.code, not e.key — ⌥/⇧ mutate e.key.

    A non-empty file REPLACES the built-in table wholesale — it does NOT merge, which is
    what lets it drop a shipped binding. The cost is the other direction: a key added to
    DEFAULT_HOTKEYS later never reaches a machine that already has this file, so re-add
    it by hand. Currently that means ``"S-Enter": "\\n"`` / ``"S-NumpadEnter": "\\n"``
    (Shift+Enter → newline instead of submit); without them Shift+Enter still submits.

    Example ~/.conductor/hotkeys.json:
      {"M-ArrowLeft": "\\u0001", "A-Backspace": "\\u001b\\u007f"}
    """
    try:
        data = json.loads(settings.hotkeys_file.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and k and v}
