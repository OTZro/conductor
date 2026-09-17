"""scroll_tmux: one batched tmux invocation per gesture, position read back from tmux.

Both regimes ship in that one invocation — every command carries a tmux-side guard, so
the pane's `#{alternate_on}` decides which ones actually run and nothing here has to ask
first. The stub records EVERY call, so the guards themselves are what these tests assert.
"""

import re

import pytest

from conductor.actions import terminal as term
from conductor.config import settings

# What the trailing read reports: "#{alternate_on} #{pane_in_mode} #{scroll_position}".
# tmux expands scroll_position to EMPTY outside copy-mode, hence the two-field replies.
LIVE = b"0 0 \n"  # normal buffer, not in copy-mode
ALT_LIVE = b"1 0 \n"  # full-screen app, not in copy-mode


def stub_run(monkeypatch, out: bytes, rc: int = 0):
    """Stub _run, recording every argv it is handed — no call is filtered out, so a test
    that asserts `calls == []` really is asserting that tmux was never contacted."""
    calls: list[list[str]] = []

    async def fake_run(argv, host=None, **kw):
        calls.append(argv)
        return rc, out

    monkeypatch.setattr(term, "_run", fake_run)
    return calls


def split_commands(argv: list[str]) -> list[list[str]]:
    """tmux argv → the individual commands its bare ';' separators delimit."""
    out: list[list[str]] = [[]]
    for a in argv[1:]:  # argv[0] is the tmux binary
        out.append([]) if a == ";" else out[-1].append(a)
    return out


def guard(cond: str, inner: str) -> list[str]:
    return ["if", "-F", "-t", "s", cond, inner]


ALT = "#{alternate_on}"  # runs only on a full-screen app
NORM = "#{?alternate_on,0,1}"  # runs only off the alt screen
NORM_IN_MODE = "#{?alternate_on,0,#{pane_in_mode}}"  # …and only inside copy-mode
READ = ["display-message", "-p", "-t", "s", "#{alternate_on} #{pane_in_mode} #{scroll_position}"]


@pytest.fixture(autouse=True)
def _pin_page_mode(monkeypatch):
    """These pin the PAGE regime for an alternate-screen pane. Without this they would
    flip with whatever CONDUCTOR_ALT_SCROLL_MODE a developer has in their own .env —
    settings reads that file, so the suite would assert one behaviour on one machine
    and another elsewhere."""
    monkeypatch.setattr(settings, "alt_scroll_mode", "page")


@pytest.mark.asyncio
async def test_up_batches_every_line_into_one_invocation(monkeypatch):
    """A gesture is one round trip and one pane redraw — sending a line at a time is
    what made scrolling lag behind the wheel. The alt and normal commands ride along
    together; tmux picks, so there is no separate probe to race with."""
    calls = stub_run(monkeypatch, b"0 1 12\n")
    state = await term.scroll_tmux("s", "up", None, 12)

    assert len(calls) == 1
    cmds = split_commands(calls[0])
    assert cmds[0] == guard(ALT, "send-keys -t s PageUp")
    assert cmds[1] == guard(NORM, "copy-mode -t s")  # works from the live pane
    assert cmds[2] == guard(NORM, "send-keys -t s -X -N 12 scroll-up")  # -N repeats in one command
    assert cmds[-1] == READ
    assert state == {"in_mode": True, "scroll": 12, "alt": False}


@pytest.mark.asyncio
async def test_down_does_not_re_enter_copy_mode(monkeypatch):
    """Scrolling down is gated on tmux's own copy-mode flag: `send-keys -X` errors on a
    live pane, and a failing command would abort the rest of the list — including the
    state read — turning a no-op into an outage."""
    calls = stub_run(monkeypatch, b"0 1 4\n")
    state = await term.scroll_tmux("s", "down", None, 3)

    cmds = split_commands(calls[0])
    assert cmds[0] == guard(ALT, "send-keys -t s PageDown")
    assert cmds[1] == guard(NORM_IN_MODE, "send-keys -t s -X -N 3 scroll-down")
    assert state == {"in_mode": True, "scroll": 4, "alt": False}


@pytest.mark.asyncio
async def test_landing_at_the_bottom_leaves_copy_mode(monkeypatch):
    """Sitting in copy-mode at the bottom swallows typing as copy-mode commands,
    which reads as a frozen claude."""
    calls = stub_run(monkeypatch, b"0 1 0\n")
    state = await term.scroll_tmux("s", "down", None, 40)

    assert calls[-1][1:] == ["send-keys", "-t", "s", "-X", "cancel"]
    assert state == {"in_mode": False, "scroll": 0, "alt": False}


@pytest.mark.asyncio
async def test_an_up_that_could_not_move_also_leaves_copy_mode(monkeypatch):
    """A pane with no scrollback reports `1 0`: in copy-mode, not moved. Staying there
    swallows typing while reporting a depth the browser reads as live — so `in_mode`
    and `scroll > 0` must never disagree, whichever direction asked."""
    calls = stub_run(monkeypatch, b"0 1 0\n")
    state = await term.scroll_tmux("s", "up", None, 1)

    assert calls[-1][1:] == ["send-keys", "-t", "s", "-X", "cancel"]
    assert state == {"in_mode": False, "scroll": 0, "alt": False}


@pytest.mark.asyncio
async def test_exit_returns_to_the_live_prompt(monkeypatch):
    calls = stub_run(monkeypatch, LIVE)
    state = await term.scroll_tmux("s", "exit", None, 1)

    assert split_commands(calls[0])[0] == guard("#{pane_in_mode}", "send-keys -t s -X cancel")
    assert state == {"in_mode": False, "scroll": 0, "alt": False}


def _scroll_repeat(argv: list[str]) -> int:
    """The `-N <n>` count on the one guarded scroll-up command."""
    inner = next(c for c in argv if "scroll-up" in c)
    return int(re.search(r"-N (\d+)", inner).group(1))


@pytest.mark.asyncio
async def test_line_count_is_clamped_and_never_zero(monkeypatch):
    calls = stub_run(monkeypatch, b"0 1 1\n")
    await term.scroll_tmux("s", "up", None, 0)
    assert _scroll_repeat(calls[0]) == 1  # never zero

    calls = stub_run(monkeypatch, b"0 1 200\n")
    await term.scroll_tmux("s", "up", None, 10_000)
    assert _scroll_repeat(calls[0]) == 200  # clamped


@pytest.mark.asyncio
async def test_a_failed_tmux_call_raises_instead_of_claiming_live(monkeypatch):
    """An unreachable remote tells us nothing about the pane. Answering `{in_mode: false}`
    would have the browser drop the badge and stop intercepting input while tmux may
    still be in copy-mode — and a 200 skips the caller's recovery path entirely."""
    stub_run(monkeypatch, b"", rc=1)
    with pytest.raises(RuntimeError):
        await term.scroll_tmux("s", "up", None, 3)


@pytest.mark.asyncio
async def test_unparseable_output_is_a_failure_too(monkeypatch):
    stub_run(monkeypatch, b"", rc=0)
    with pytest.raises(RuntimeError):
        await term.scroll_tmux("s", "up", None, 1)


@pytest.mark.asyncio
async def test_a_truncated_read_is_a_failure_too(monkeypatch):
    """One field back is not a state: `alternate_on` alone can't say whether copy-mode is
    open, and defaulting it to "no" is the same lie a failed call would tell."""
    stub_run(monkeypatch, b"0\n", rc=0)
    with pytest.raises(RuntimeError):
        await term.scroll_tmux("s", "up", None, 1)


@pytest.mark.asyncio
async def test_unknown_direction_touches_nothing(monkeypatch):
    """Validated before tmux is contacted, so a bad direction stays a pure no-op instead
    of an exec that can fail and surface as a 502."""
    calls = stub_run(monkeypatch, b"0 1 5\n")
    assert await term.scroll_tmux("s", "sideways", None, 3) == {"in_mode": False, "scroll": 0, "alt": False}
    assert calls == []


# ── full-screen app (claude v2) on the ALTERNATE screen ───────────────────────────
# tmux copy-mode can't scroll it — the app pages itself with its own PageUp/PageDown.
# The commands sent are identical to the normal-buffer case above; what differs is which
# of them tmux's guards let run, and the `alt` the trailing read hands back.


@pytest.mark.asyncio
async def test_alt_screen_is_reported_from_the_pane_not_guessed(monkeypatch):
    """`alt` comes from `#{alternate_on}` in the same read that reports copy-mode, so it
    describes the pane the keys just went to — not one sampled before them."""
    stub_run(monkeypatch, ALT_LIVE)
    assert await term.scroll_tmux("s", "up", None, 120) == {"in_mode": False, "scroll": 0, "alt": True}


@pytest.mark.asyncio
async def test_alt_screen_pages_once_per_request(monkeypatch):
    """One `PageUp`, whatever the gesture's line count: a page is a whole screen and there
    is no depth reading here to correct an overshoot with. Rounding 60 lines up to 2 pages
    is how a fling used to travel ~400 lines; the client re-requests to go further."""
    calls = stub_run(monkeypatch, ALT_LIVE)
    await term.scroll_tmux("s", "up", None, 120)

    assert len(calls) == 1
    assert sum(any("PageUp" in a for a in cmd) for cmd in split_commands(calls[0])) == 1


@pytest.mark.asyncio
async def test_alt_screen_down_sends_pagedown(monkeypatch):
    calls = stub_run(monkeypatch, ALT_LIVE)
    state = await term.scroll_tmux("s", "down", None, 40)

    cmds = split_commands(calls[0])
    assert cmds[0] == guard(ALT, "send-keys -t s PageDown")
    assert state == {"in_mode": False, "scroll": 0, "alt": True}


@pytest.mark.asyncio
async def test_the_copy_mode_keys_are_guarded_off_the_alt_screen(monkeypatch):
    """The scroll-ups ride in the same invocation as the PageUp, so they MUST carry a
    guard that is false on a full-screen pane — otherwise claude's own screen gets
    dragged into tmux copy-mode, which is exactly what this fix set out to stop."""
    calls = stub_run(monkeypatch, ALT_LIVE)
    await term.scroll_tmux("s", "up", None, 3)

    for cmd in split_commands(calls[0])[:-1]:
        inner = cmd[-1]
        assert cmd[:4] == ["if", "-F", "-t", "s"]
        assert cmd[4] == ALT if ("PageUp" in inner or "PageDown" in inner) else cmd[4] == NORM


@pytest.mark.asyncio
async def test_exit_still_cancels_copy_mode_on_a_full_screen_pane(monkeypatch):
    """A pane can be full-screen AND in tmux copy-mode at once. Skipping the cancel there
    hands teardown a pane stuck in copy-mode with the badge cleared — the frozen-claude
    shape. The cancel is already gated on `#{pane_in_mode}`, so it costs nothing."""
    calls = stub_run(monkeypatch, b"1 1 0\n")
    state = await term.scroll_tmux("s", "exit", None, 1)

    assert split_commands(calls[0])[0] == guard("#{pane_in_mode}", "send-keys -t s -X cancel")
    # reported in copy-mode at depth 0 → left, so `in_mode ⇒ scroll > 0` still holds
    assert calls[-1][1:] == ["send-keys", "-t", "s", "-X", "cancel"]
    assert state == {"in_mode": False, "scroll": 0, "alt": True}


@pytest.mark.asyncio
async def test_a_failed_page_raises_instead_of_reporting_a_scroll(monkeypatch):
    """The send and the read are one invocation under one status, so a page that never
    landed cannot come back as a successful scroll — which would have the client drain
    the rest of a fling into more silent no-ops with no error surfaced."""
    stub_run(monkeypatch, b"", rc=1)
    with pytest.raises(RuntimeError):
        await term.scroll_tmux("s", "up", None, 5)
