"""Named launch presets for a card's claude session.

A LOCAL, user-owned JSON at ``~/.conductor/profiles.json`` (path via
``CONDUCTOR_LAUNCH_PROFILES_FILE``) — same pattern as prompts.json / termkeys.json:
the repo ships nothing, each user keeps their own outside version control. Absent or
corrupt → no profiles (the feature is simply off; launches behave exactly as before).

Each profile bundles what a NEW ("own") session launches with::

    {
      "review": {
        "host": "base",                        // base | <remote name> | omit (local)
        "cwd": "~/code",                        // working dir (~ ok); omit → configured default
        "env": {                                // env vars the launched claude inherits
          "CLAUDE_CONFIG_DIR": "~/.claude-review"
        }
      }
    }

Only ``own`` sessions read a profile; resume pins the conversation's own host + cwd.
Precedence is applied by the API: explicit request fields > profile > defaults. ``env``
injection is LOCAL-only for now (ssh does not carry ``env=`` to a remote claude — see
actions/terminal.py, which rejects remote + env until the inline-prefix path lands).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)


def _load() -> dict:
    """Raw {name: spec} from the file, or {} when absent/corrupt (warned once per read)."""
    try:
        data = json.loads(Path(settings.launch_profiles_file).read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:  # malformed JSON / unreadable
        log.warning("launch profiles unreadable (%s) — ignoring", exc)
        return {}
    if not isinstance(data, dict):
        log.warning("launch profiles must be a JSON object of name→spec — ignoring")
        return {}
    return {str(name): spec for name, spec in data.items() if isinstance(spec, dict)}


def _norm(spec: dict) -> dict:
    """A raw spec → ``{host: str|None, cwd: str|None, env: {str: str}}`` (all coerced)."""
    raw_env = spec.get("env")
    return {
        "host": str(spec["host"]) if spec.get("host") else None,
        "cwd": str(spec["cwd"]) if spec.get("cwd") else None,
        "env": {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {},
    }


def resolve(name: str) -> dict | None:
    """One profile normalized to ``{host, cwd, env}``, or None if the name is unknown."""
    spec = _load().get(name)
    return _norm(spec) if spec is not None else None


def summaries() -> list[dict]:
    """``[{name, host, cwd, env}]`` for the New-session picker — the FE shows host/cwd on
    the pill and lists env in its tooltip. (env values are shown verbatim; a single-user
    localhost tool, so a profile that carries a secret would surface it on hover.)"""
    return [{"name": name, **_norm(spec)} for name, spec in _load().items()]
