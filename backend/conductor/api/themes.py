from __future__ import annotations

import json
import re

from fastapi import APIRouter

from ..config import settings

router = APIRouter(prefix="/api", tags=["themes"])

# A theme is data, not code: a set of CSS custom properties for the UI, an xterm palette
# for the embedded terminal, and optional tmux styles. Anything the frontend's built-in
# dark/light pair can express, a user-supplied theme can too.
_BASES = {"dark", "light"}
_VAR_RE = re.compile(r"^--[A-Za-z0-9_-]{1,40}$")
# "9 9 11" — space-separated RGB channels, the form tailwind.config.js consumes as
# `rgb(var(--x) / <alpha-value>)`. A hex here would silently break every opacity modifier.
_CHANNELS_RE = re.compile(r"^\d{1,3} \d{1,3} \d{1,3}$")
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
# tmux style options we let a theme set, and the shape of a tmux style value
# ("fg=#d8dee9,bg=#3b4252,none"). Both are allowlists: these values are forwarded to a
# `tmux set-option` argv, so an unbounded string would be an injection surface.
_TMUX_OPTIONS = {
    "status-style", "status-left-style", "status-right-style",
    "window-status-style", "window-status-current-style",
    "pane-border-style", "pane-active-border-style",
    "message-style", "mode-style",
}
_TMUX_VALUE_RE = re.compile(r"^[A-Za-z0-9#,=_ -]{1,200}$")


def _clean_map(raw: object, key_re: re.Pattern | None, value_re: re.Pattern,
                allowed_keys: set[str] | None = None) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if allowed_keys is not None and k not in allowed_keys:
            continue
        if key_re is not None and not key_re.match(k):
            continue
        if not value_re.match(v):
            continue
        out[k] = v
    return out


def parse_theme(raw: object) -> dict | None:
    """One user theme → a validated dict, or None if it can't be trusted. Pure, so the
    shape is unit-checkable and the endpoint stays a thin file read."""
    if not isinstance(raw, dict):
        return None
    tid, label = raw.get("id"), raw.get("label")
    if not isinstance(tid, str) or not tid.strip():
        return None
    if not isinstance(label, str) or not label.strip():
        return None
    base = raw.get("base") if raw.get("base") in _BASES else "dark"
    return {
        "id": tid.strip(),
        "label": label.strip(),
        "base": base,
        # `vars` overrides the base ramp; a theme may set only the accents it cares about
        "vars": _clean_map(raw.get("vars"), _VAR_RE, _CHANNELS_RE),
        "terminal": _clean_map(raw.get("terminal"), None, _HEX_RE),
        "tmux": _clean_map(raw.get("tmux"), None, _TMUX_VALUE_RE, allowed_keys=_TMUX_OPTIONS),
        "source": "file",
    }


@router.get("/themes")
async def list_themes() -> list[dict]:
    """User-defined themes, read from an OPTIONAL local file (CONDUCTOR_THEMES_FILE,
    default ~/.conductor/themes.json — OUTSIDE the repo), the same user-owned-file
    pattern as termkeys.json / lanes.json / prompts.json. Absent/invalid → ``[]``, and
    the frontend still has its built-in dark + light pair.

    Each entry:
      {"id","label","base":"dark"|"light",
       "vars":    {"--z-950":"46 52 64", …},   # space-separated RGB channels
       "terminal":{"background":"#2e3440", …}, # xterm palette, hex
       "tmux":    {"status-style":"fg=#d8dee9,bg=#3b4252"}}

    Every field is allowlisted and format-checked here: `vars`/`terminal` reach CSS and
    ttyd, and `tmux` reaches a tmux set-option argv, so a malformed or hostile entry is
    dropped rather than forwarded.
    """
    try:
        data = json.loads(settings.themes_file.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out, seen = [], set()
    for raw in data:
        theme = parse_theme(raw)
        if theme and theme["id"] not in seen:
            seen.add(theme["id"])
            out.append(theme)
    return out
