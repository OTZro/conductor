from __future__ import annotations

import asyncio
import logging
import sys

from .config import settings

log = logging.getLogger("conductor.notify")

# Suppress notifications during the initial seed (tailer replays history + first
# poll pulls all held tickets) — otherwise boot would fire a burst. main() arms
# this after the first poll cycle completes.
_armed = False

# strong refs to in-flight plugin-channel tasks — the loop keeps only a weak ref to a
# task, so a discarded create_task return could be GC'd before delivering the ping.
_channel_tasks: set[asyncio.Task] = set()


def _fan_out(ctx: dict) -> None:
    """Deliver a need-human event to every plugin's NotifySpec channels — how a plugin
    adds a notification medium (Slack DM, Telegram, …) beside the built-in macOS/ntfy
    pings. Fire-and-forget, timeout-bounded, error-isolated: a broken channel logs and
    is dropped, never blocks or breaks the others."""
    from .plugins import runtime  # lazy — plugins import conductor modules importing this

    for plugin_id, spec in runtime.spec_rows("notifiers"):

        async def _call(cb=spec.callback, pid=plugin_id):
            try:
                await asyncio.wait_for(cb(dict(ctx)), 10.0)
            except Exception as exc:  # noqa: BLE001 — one channel must not kill the rest
                log.warning("[notify] plugin %s channel failed: %s", pid, exc)

        task = asyncio.create_task(_call())
        _channel_tasks.add(task)
        task.add_done_callback(_channel_tasks.discard)


def arm() -> None:
    global _armed
    _armed = True


def _deep_link(external_id: str | None) -> str | None:
    """Shareable URL to the card (§3C.4) — the notification is a dead end without
    it. Needs public_url (the tailscale-serve origin the phone can reach)."""
    if not external_id or not settings.public_url:
        return None
    return f"{settings.public_url.rstrip('/')}/?card={external_id}"


async def need_human(title: str, subtitle: str = "", external_id: str | None = None) -> None:
    """Best-effort ping when a card newly lands in Need Human. macOS Notification
    Center via osascript; plus an ntfy topic if CONDUCTOR_NTFY_TOPIC is set; plus
    every plugin-declared NotifySpec channel. All carry a deep-link to the card when
    public_url is configured."""
    if not _armed:
        return
    link = _deep_link(external_id)
    _fan_out(
        {"event": "need_human", "title": title, "subtitle": subtitle,
         "ref": external_id, "link": link}
    )

    if sys.platform == "darwin":
        msg = (title or "").replace('"', "'")[:200]
        sub = (subtitle or "").replace('"', "'")[:120]
        script = (
            f'display notification "{msg}" with title "Conductor — Need Human" '
            f'subtitle "{sub}"'
        )
        try:
            p = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
        except Exception:  # noqa: BLE001
            pass

    if settings.ntfy_topic:
        try:
            import httpx

            headers = {"Title": "Conductor — Need Human"}
            if link:
                headers["Click"] = link  # ntfy: tapping the push opens the card
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"https://ntfy.sh/{settings.ntfy_topic}",
                    content=f"{title} — {subtitle}".encode(),
                    headers=headers,
                )
        except Exception:  # noqa: BLE001
            pass
