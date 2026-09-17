"""Unit tests for terminal session/workspace construction (no ttyd/tmux spawned)."""

import json
from pathlib import Path

import pytest

from conductor.actions import agent_state
from conductor.actions import terminal as term


def test_workspace_for_uses_default_workspace_root(monkeypatch, tmp_path):
    monkeypatch.setattr(term.settings, "default_workspace_root", tmp_path)
    assert term.workspace_for("jira", "NOPE-99999") == tmp_path


def test_workspace_for_falls_back_to_home_when_default_root_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(term.settings, "default_workspace_root", tmp_path / "does-not-exist")
    assert term.workspace_for("jira", "NOPE-99999") == Path.home()


def test_default_cwd_renders_configured_default_home_relative(monkeypatch, tmp_path):
    """The New-session pre-fill reflects CONDUCTOR_DEFAULT_WORKSPACE_ROOT (not a
    hardcoded path) with $HOME collapsed to ~ so it stays host-relative."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "code").mkdir()
    monkeypatch.setattr(term.settings, "default_workspace_root", tmp_path / "code")
    # manual card → no explicit override → falls through to the configured default
    assert term.default_cwd("manual", "whatever") == "~/code"


@pytest.mark.asyncio
async def test_list_dirs_validity_and_prefix_completions(monkeypatch):
    async def fake_run(argv, host=None, **kw):
        # `test -d … && printf V` (dir exists) + `ls -1F` of the parent (dirs end in '/')
        return 0, b"V\nfms/\nfoo/\nnotes.txt\n.git/\n"

    monkeypatch.setattr(term, "_run", fake_run)
    res = await term.list_dirs("~/code/f", host=None)
    assert res["valid"] is True
    # dirs only, filtered by the 'f' leaf, re-prefixed with the verbatim typed parent
    assert res["dirs"] == ["~/code/fms", "~/code/foo"]


@pytest.mark.asyncio
async def test_list_dirs_invalid_when_no_marker(monkeypatch):
    async def fake_run(argv, host=None, **kw):
        return 0, b"code/\ncodex/\n"  # no leading 'V' → the path itself is not a dir

    monkeypatch.setattr(term, "_run", fake_run)
    res = await term.list_dirs("~/nope", host=None)
    assert res["valid"] is False
    assert res["dirs"] == []  # nothing under ~ starts with 'nope'


@pytest.mark.asyncio
async def test_list_dirs_preserves_filesystem_root(monkeypatch):
    """An absolute path like '/u' must list the ROOT ('/'), not fall back to $HOME."""
    seen = {}

    async def fake_run(argv, host=None, **kw):
        seen["script"] = argv[-1]  # ["sh", "-c", <script>]
        return 0, b"Users/\nusr/\nbin/\n"

    monkeypatch.setattr(term, "_run", fake_run)
    res = await term.list_dirs("/u", host=None)
    assert "-- / 2>/dev/null" in seen["script"]  # parent is '/', not "$HOME"
    assert res["dirs"] == ["/Users", "/usr"]  # '/' + names filtered by 'u' (case-insensitive)


# ── host runner (base/roam) ──────────────────────────────────────────────────


@pytest.fixture
def roam(monkeypatch):
    monkeypatch.setattr(term.settings, "remote_hosts", "roam=mbp.tail.ts.net")
    monkeypatch.setattr(term.settings, "local_host_name", "base")
    return "roam"


def test_ssh_target_local_forms(roam):
    assert term.ssh_target(None) is None
    assert term.ssh_target("") is None
    assert term.ssh_target("base") is None  # local name = the degenerate case


def test_ssh_target_remote_and_unknown(roam):
    assert term.ssh_target("roam") == "mbp.tail.ts.net"
    with pytest.raises(RuntimeError, match="unknown host"):
        term.ssh_target("nope")


def test_sh_path_expands_only_leading_tilde():
    assert term._sh_path("~") == '"$HOME"'
    assert term._sh_path("~/code my dir") == '"$HOME"/' + "'code my dir'"
    assert term._sh_path("/tmp/x") == "/tmp/x"  # shlex leaves safe strings bare
    assert "'" in term._sh_path("/tmp/a b")  # unsafe → quoted


def test_remote_argv_login_shell_and_tty():
    plain = term.remote_argv("mbp.tail.ts.net", "tmux ls")
    assert plain[0] == "ssh" and "mbp.tail.ts.net" in plain
    assert plain[-1] == "exec \"$SHELL\" -l -c 'tmux ls'"  # login shell → user PATH
    assert "-t" not in plain
    tty = term.remote_argv("mbp.tail.ts.net", "tmux attach", tty=True)
    assert "-t" in tty and "ServerAliveInterval=15" in " ".join(tty)


def test_remote_cwd_defaults_and_home_relative(monkeypatch):
    monkeypatch.setattr(term.settings, "remote_workspace_root", "~/work")
    assert term._remote_cwd(None) == "~/work"
    assert term._remote_cwd("  ") == "~/work"
    assert term._remote_cwd("~/code") == "~/code"
    assert term._remote_cwd("/abs") == "/abs"
    assert term._remote_cwd("code/x") == "~/code/x"  # bare relative → home-relative


def test_hook_cfg_urls():
    cfg = term._hook_cfg("card1", "http://127.0.0.1:8787")
    cmd = cfg["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    assert "http://127.0.0.1:8787/api/cards/card1/agent" in cmd
    cfg = term._hook_cfg("card1", "https://base.tail.ts.net")
    cmd = cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "https://base.tail.ts.net/api/cards/card1/agent" in cmd


def test_hook_cfg_never_reports_waiting():
    """'waiting' must stay pane-poll-owned: no hook may POST a waiting STATE (Stop
    is the offender, and would bounce a bg-task turn). The Notification hook is
    allowed back — it forwards its message to /notify, not a running/waiting state."""
    cfg = term._hook_cfg("card1", "http://127.0.0.1:8787")
    assert "Stop" not in cfg["hooks"]
    assert "waiting" not in json.dumps(cfg)  # no hook writes the waiting state


def test_hook_cfg_notification_forwards_message_not_state():
    cfg = term._hook_cfg("card1", "http://127.0.0.1:8787")
    ncmd = cfg["hooks"]["Notification"][0]["hooks"][0]["command"]
    assert "/api/cards/card1/notify" in ncmd
    assert "--data-binary @-" in ncmd  # forwards the hook's stdin (message) verbatim
    assert "state" not in ncmd  # it's a message, not a running/waiting signal


# ── hook-settings dir hardening (world-writable /tmp; claude runs the curls inside) ──


def test_ensure_hook_dir_creates_it_0700(monkeypatch, tmp_path):
    import stat as stat_mod

    d = tmp_path / "hooks"
    monkeypatch.setattr(term, "_HOOK_DIR", d)
    term._ensure_hook_dir()
    assert d.is_dir()
    assert stat_mod.S_IMODE(d.stat().st_mode) == 0o700  # owner-only
    term._ensure_hook_dir()  # idempotent: a second call on our own dir is fine
    assert stat_mod.S_IMODE(d.stat().st_mode) == 0o700


def test_ensure_hook_dir_refuses_a_symlink(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "hooks"
    link.symlink_to(real)  # a same-host account could plant this under /tmp
    monkeypatch.setattr(term, "_HOOK_DIR", link)
    with pytest.raises(RuntimeError, match="symlink"):
        term._ensure_hook_dir()


# ── pane classification: activity-sustain (thinking / subagent-team frames) ─────


def _fake_pane(monkeypatch, texts: list[str]):
    """_run returns each text in turn (the pane as captured on successive polls)."""
    it = iter(texts)

    async def fake_run(argv, host=None, input_=None, timeout=8):
        return 0, next(it).encode()

    monkeypatch.setattr(agent_state, "_run", fake_run)


THINKING_FRAME_1 = "∴ Thinking…\n  12s\n❯\n  auto mode on"
THINKING_FRAME_2 = "∴ Thinking…\n  17s\n❯\n  auto mode on"  # timer ticked
IDLE_FRAME = "❯\n  fms main ~71 Fable 5 ctx:16%\n  auto mode on"
SPINNER_FRAME = "✻ Boogieing… (3m 45s · ↓ 2.0k tokens · esc to interrupt)\n❯"


@pytest.mark.asyncio
async def test_classify_sustains_running_while_screen_repaints(monkeypatch):
    """No spinner match, but the pane changed between polls (thinking timer,
    subagent rows) → a running card STAYS running instead of bouncing."""
    term._PANE_SEEN.clear()
    _fake_pane(monkeypatch, [THINKING_FRAME_1, THINKING_FRAME_2])
    first = await term._classify_pane("s", None, sustain_running=True)
    assert first == ("waiting", None, None)  # first sight: no baseline yet
    second = await term._classify_pane("s", None, sustain_running=True)
    assert second[0] == "running"  # screen repainted → work in flight


@pytest.mark.asyncio
async def test_classify_static_idle_screen_is_waiting(monkeypatch):
    term._PANE_SEEN.clear()
    _fake_pane(monkeypatch, [IDLE_FRAME, IDLE_FRAME])
    await term._classify_pane("s", None, sustain_running=True)
    second = await term._classify_pane("s", None, sustain_running=True)
    assert second == ("waiting", None, None)  # identical frames → genuinely idle


@pytest.mark.asyncio
async def test_classify_no_sustain_for_cards_not_running(monkeypatch):
    """Screen activity alone must not PROMOTE an idle card (e.g. a human typing
    in the terminal) — sustain only bridges an already-running card's gaps."""
    term._PANE_SEEN.clear()
    _fake_pane(monkeypatch, [THINKING_FRAME_1, THINKING_FRAME_2])
    await term._classify_pane("s", None, sustain_running=False)
    second = await term._classify_pane("s", None, sustain_running=False)
    assert second == ("waiting", None, None)


@pytest.mark.asyncio
async def test_classify_spinner_still_wins(monkeypatch):
    term._PANE_SEEN.clear()
    _fake_pane(monkeypatch, [SPINNER_FRAME])
    assert (await term._classify_pane("s", None))[0] == "running"


@pytest.mark.asyncio
async def test_build_command_own_remote_wraps_in_ssh(roam):
    # card_id="" skips the hook push, so no ssh happens during the test
    cmd, writable, name, cwd, sid = await term._build_command(
        "own", "jira", "PROJ-1", "s1", "", "~/code dir", None, None, "roam"
    )
    assert writable and name == f"conductor-{sid[:8]}"
    assert cmd[0] == "ssh" and "mbp.tail.ts.net" in cmd
    inner = cmd[-1]
    assert inner.startswith('exec "$SHELL" -l -c ')
    assert "tmux new-session -A -s" in inner
    assert '"$HOME"/' in inner  # ~ expands on the REMOTE, not here
    assert cwd == "~/code dir"


@pytest.mark.asyncio
async def test_build_command_own_local_unchanged(roam):
    cmd, writable, name, cwd, sid = await term._build_command(
        "own", "jira", "PROJ-1", "s1", "", None, None, None, None
    )
    assert cmd[0] == "tmux" and writable and name == f"conductor-{sid[:8]}"


@pytest.mark.asyncio
async def test_resume_rejected_when_conversation_absent_on_host(roam, monkeypatch):
    """A conversation only resumes where its transcript (or live tmux) is — a
    wrong-host resume must fail loudly, not spawn a claude that errors out."""
    async def no(*a, **k):
        return False

    monkeypatch.setattr(term, "_tmux_has_session", no)
    monkeypatch.setattr(term, "_conversation_exists", no)
    with pytest.raises(RuntimeError, match="not found on roam"):
        await term._build_command(
            "resume", "jira", "PROJ-1", "s1", "", None, "abcd1234-sid", None, "roam"
        )


@pytest.mark.asyncio
async def test_resume_allowed_by_live_tmux_or_transcript(roam, monkeypatch):
    async def yes(*a, **k):
        return True

    async def no(*a, **k):
        return False

    async def nocwd(*a, **k):
        return None

    monkeypatch.setattr(term, "_conversation_cwd", nocwd)  # don't hit the real fs/ssh
    monkeypatch.setattr(term, "_tmux_has_session", no)
    monkeypatch.setattr(term, "_conversation_exists", yes)  # transcript there
    cmd, *_ = await term._build_command(
        "resume", "jira", "PROJ-1", "s1", "", None, "abcd1234-sid", None, None
    )
    assert cmd[0] == "tmux"
    monkeypatch.setattr(term, "_tmux_has_session", yes)  # live tmux there
    monkeypatch.setattr(term, "_conversation_exists", no)
    cmd, *_ = await term._build_command(
        "resume", "jira", "PROJ-1", "s1", "", None, "abcd1234-sid", None, "roam"
    )
    assert cmd[0] == "ssh"


@pytest.mark.asyncio
async def test_resume_launches_in_conversation_recorded_cwd(roam, monkeypatch, tmp_path):
    """claude --resume resolves a session by cwd, so the resume must launch in the
    dir the conversation was created in — not resolve_cwd's default workspace /
    remote_workspace_root. Regression for PROJ-6595 ('No conversation found' on both
    base and roam even though both transcripts existed)."""
    # A REAL directory, because the local branch guards the override on
    # `Path(conv_cwd).is_dir()`. Hardcoding an absolute path here (this test used to
    # name one developer's checkout) makes the assertion pass only on the machine
    # where that dir happens to exist, and silently fall through to resolve_cwd
    # everywhere else.
    conv = tmp_path / "code" / "fms"
    conv.mkdir(parents=True)
    conv_dir = str(conv)

    async def yes(*a, **k):
        return True

    async def no(*a, **k):
        return False

    async def conv_cwd(sid, host=None):
        return conv_dir

    monkeypatch.setattr(term, "_tmux_has_session", no)
    monkeypatch.setattr(term, "_conversation_exists", yes)
    monkeypatch.setattr(term, "_conversation_cwd", conv_cwd)

    # local: the -c dir is the conversation's cwd, overriding resolve_cwd
    cmd, _w, _n, cwd, _s = await term._build_command(
        "resume", "jira", "PROJ-6595", "s1", "", None, "d258a66a-sid", None, None
    )
    assert cwd == conv_dir
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == conv_dir

    # remote: same cwd flows into the ssh inner command. No is_dir() guard on this
    # path — the dir lives on the other host's filesystem, which we cannot stat.
    cmd, _w, _n, cwd, _s = await term._build_command(
        "resume", "jira", "PROJ-6595", "s1", "", None, "294f0545-sid", None, "roam"
    )
    assert cwd == conv_dir
    assert conv_dir in cmd[-1]


@pytest.mark.asyncio
async def test_resume_falls_back_when_recorded_cwd_is_gone(roam, monkeypatch, tmp_path):
    """The recorded cwd can outlive the directory (worktree removed, repo moved). Pin
    to it only when it still exists locally, otherwise fall back to resolve_cwd — a
    `tmux -c` at a missing path fails to launch at all. The remote branch keeps
    trusting the recorded path: it is on a filesystem this host cannot stat."""
    gone = str(tmp_path / "deleted-worktree")  # never created

    async def yes(*a, **k):
        return True

    async def no(*a, **k):
        return False

    async def conv_cwd(sid, host=None):
        return gone

    monkeypatch.setattr(term, "_tmux_has_session", no)
    monkeypatch.setattr(term, "_conversation_exists", yes)
    monkeypatch.setattr(term, "_conversation_cwd", conv_cwd)

    _cmd, _w, _n, cwd, _s = await term._build_command(
        "resume", "jira", "PROJ-6595", "s1", "", None, "d258a66a-sid", None, None
    )
    assert cwd == str(term.resolve_cwd("jira", "PROJ-6595", None))

    _cmd, _w, _n, cwd, _s = await term._build_command(
        "resume", "jira", "PROJ-6595", "s1", "", None, "294f0545-sid", None, "roam"
    )
    assert cwd == gone


@pytest.mark.asyncio
async def test_build_command_local_wraps_pane_with_env(roam, monkeypatch):
    """Profile env must reach the PANE, so it's wrapped as `env K=V claude …` INSIDE the
    tmux command — an already-running tmux server ignores the client env, so env= on ttyd
    would be silently dropped. Values are expanded now (no shell runs the local argv)."""
    monkeypatch.setenv("HOME", "/home/x")
    cmd, *_ = await term._build_command(
        "own", "jira", "PROJ-1", "s1", "", None, "fixed-sid", None, None, False,
        {"CLAUDE_CONFIG_DIR": "~/.claude-x"},
    )
    assert "env" in cmd
    i = cmd.index("env")
    assert cmd[i + 1] == "CLAUDE_CONFIG_DIR=/home/x/.claude-x"  # ~ expanded to a literal
    assert cmd[i + 2] == term.settings.claude_bin  # env(1) wraps claude directly


@pytest.mark.asyncio
async def test_build_command_local_no_env_no_wrapper(roam):
    """No profile env → no `env` wrapper; the pane command is claude directly."""
    cmd, *_ = await term._build_command(
        "own", "jira", "PROJ-1", "s1", "", None, "fixed-sid", None, None, False, None,
    )
    assert "env" not in cmd and term.settings.claude_bin in cmd


@pytest.mark.asyncio
async def test_build_command_remote_wraps_pane_with_env(roam):
    """Remote gets the same `env K=V` wrapper inside the ssh shell line, keeping ~ for the
    REMOTE shell to expand (remote $HOME differs) — previously this was rejected outright."""
    cmd, *_ = await term._build_command(
        "own", "jira", "PROJ-1", "s1", "", None, "fixed-sid", None, "roam", False,
        {"CLAUDE_CONFIG_DIR": "~/.claude-x"},
    )
    assert cmd[0] == "ssh"
    assert 'env CLAUDE_CONFIG_DIR="$HOME"/.claude-x' in cmd[-1]


@pytest.mark.asyncio
async def test_resume_attach_only_refuses_to_revive_dead_session(roam, monkeypatch):
    """Card-open auto-load (attach_only) must ATTACH a running session, never REVIVE a
    killed one — even when the transcript is right there. It fails so the caller falls
    through to its live-session scan instead of new-session -A resurrecting it."""
    async def no(*a, **k):
        return False

    async def yes(*a, **k):
        return True

    monkeypatch.setattr(term, "_tmux_has_session", no)  # tmux is dead
    monkeypatch.setattr(term, "_conversation_exists", yes)  # transcript still on disk
    with pytest.raises(RuntimeError, match="no live tmux to attach"):
        await term._build_command(
            "resume", "jira", "PROJ-1", "s1", "", None, "abcd1234-sid", None, None,
            True,  # attach_only
        )


@pytest.mark.asyncio
async def test_resume_attach_only_attaches_live_session_without_creating(roam, monkeypatch):
    """A live session under attach_only becomes a pure `tmux attach` (writable) — never
    `new-session -A`, which would recreate/revive it. cwd is left untouched (None)."""
    async def yes(*a, **k):
        return True

    async def no(*a, **k):
        return False

    monkeypatch.setattr(term, "_tmux_has_session", yes)  # live
    monkeypatch.setattr(term, "_conversation_exists", no)  # not consulted for attach

    # local: plain writable attach, no new-session, no cwd rewrite
    cmd, writable, name, cwd, sid = await term._build_command(
        "resume", "jira", "PROJ-1", "s1", "", None, "abcd1234-sid", None, None,
        True,  # attach_only
    )
    assert cmd == ["tmux", "attach", "-t", "conductor-abcd1234"]
    assert writable is True and name == "conductor-abcd1234"
    assert cwd is None and sid == "abcd1234-sid"
    assert "new-session" not in cmd

    # remote: ssh-wrapped attach, still never new-session
    cmd, *_ = await term._build_command(
        "resume", "jira", "PROJ-1", "s1", "", None, "abcd1234-sid", None, "roam",
        True,
    )
    assert cmd[0] == "ssh"
    assert "attach" in cmd[-1] and "new-session" not in cmd[-1]


# ── attach: always requires a plugin-declared cached.watch, no fallback ─────────


@pytest.mark.asyncio
async def test_build_command_attach_requires_watch(monkeypatch):
    """attach has no session convention of its own to fall back to — a card with no
    cached.watch (the default for a fresh install, since no shipped plugin declares
    one) must fail loudly rather than silently doing nothing."""
    with pytest.raises(RuntimeError, match="no cached.watch session to attach"):
        await term._build_command("attach", "jira", "PROJ-1", "s1", "card1", None, None)


@pytest.mark.asyncio
async def test_build_command_attach_requires_watch_with_watch_none(monkeypatch):
    """Regression: explicitly passing watch=None (what api/terminals.py sends for
    every card without a cached.watch dict) is identical to omitting it — still no
    fallback."""
    with pytest.raises(RuntimeError, match="no cached.watch session to attach"):
        await term._build_command(
            "attach", "jira", "PROJ-1", "s1", "card1", None, None, watch=None,
        )


@pytest.mark.asyncio
async def test_build_command_attach_watch_generalized_path(monkeypatch):
    """A card whose cached.watch names a live tmux (e.g. an agent-team member's claude
    pane on its own named socket) attaches to it: -L <socket>.

    Writability on a NAMED socket is the operator's per-socket policy, not the watch
    declarer's wish: this socket is unconfigured, and unconfigured means read-only —
    so even with no `writable` key (declarer default read-write) the attach is forced
    read-only. The `:rw` opt-in is pinned by the ceiling test below."""
    seen = {}

    async def fake_has_session(name, host=None, socket=None):
        seen["args"] = (name, host, socket)
        return True

    monkeypatch.setattr(term, "_tmux_has_session", fake_has_session)
    monkeypatch.setattr(term.settings, "extra_tmux_sockets", "")
    watch = {"socket": "agents", "session": "member-alice", "host": None}
    cmd, writable, name, cwd, sid = await term._build_command(
        "attach", "jira", "PROJ-1", "s1", "card1", None, None, None, watch=watch,
    )
    assert cmd == ["tmux", "-L", "agents", "attach", "-t", "member-alice", "-r", "-f", "read-only,ignore-size"]
    assert writable is False
    assert name == "member-alice"
    assert cwd is None and sid is None
    # has-session was checked on the RIGHT socket, not the default one
    assert seen["args"] == ("member-alice", None, "agents")


@pytest.mark.asyncio
async def test_watch_writability_is_capped_by_the_socket_policy(monkeypatch):
    """The operator's `<socket>:rw` is the CEILING: it lets a watch's declarer-default
    read-write stand, and a watch may still be stricter (writable=False) — but nothing
    a plugin writes can open a read-only socket up. Default-socket watches stay the
    declarer's call (those sessions are conductor's own)."""
    async def yes(*a, **k):
        return True

    monkeypatch.setattr(term, "_tmux_has_session", yes)
    monkeypatch.setattr(term.settings, "extra_tmux_sockets", "agents:rw")

    # :rw socket + no writable key → the declarer default (read-write) survives
    cmd, writable, *_ = await term._build_command(
        "attach", "jira", "PROJ-1", "s1", "card1", None, None, None,
        watch={"socket": "agents", "session": "member-alice", "host": None},
    )
    assert cmd == ["tmux", "-L", "agents", "attach", "-t", "member-alice"]
    assert writable is True

    # :rw socket + an explicitly read-only watch → stricter side wins
    cmd, writable, *_ = await term._build_command(
        "attach", "jira", "PROJ-1", "s2", "card1", None, None, None,
        watch={"socket": "agents", "session": "member-alice", "host": None, "writable": False},
    )
    assert writable is False and "-r" in cmd

    # default socket (None): no policy applies — declarer default read-write
    cmd, writable, *_ = await term._build_command(
        "attach", "jira", "PROJ-1", "s3", "card1", None, None, None,
        watch={"socket": None, "session": "member-alice", "host": None},
    )
    assert cmd == ["tmux", "attach", "-t", "member-alice"]
    assert writable is True


@pytest.mark.asyncio
async def test_build_command_attach_watch_readonly_when_writable_false(monkeypatch):
    """A source that wants a READ-ONLY watch sets cached.watch['writable']=False (e.g.
    an agent-team plugin — a member's pane has its own writer). Then tmux gets -r AND ttyd is denied
    --writable (writable=False) — the double lock the watch path used to hardcode."""
    async def yes(*a, **k):
        return True

    monkeypatch.setattr(term, "_tmux_has_session", yes)
    monkeypatch.setattr(term.settings, "extra_tmux_sockets", "")
    watch = {"socket": "agents", "session": "member-alice", "host": None, "writable": False}
    cmd, writable, name, cwd, sid = await term._build_command(
        "attach", "jira", "PROJ-1", "s1", "card1", None, None, None, watch=watch,
    )
    assert cmd == ["tmux", "-L", "agents", "attach", "-t", "member-alice", "-r", "-f", "read-only,ignore-size"]
    assert writable is False
    assert name == "member-alice"


@pytest.mark.asyncio
async def test_build_command_attach_watch_fails_when_not_live(monkeypatch):
    """attach a RUNNING session or fail, never revive — never conjure a tmux that
    isn't already there."""
    async def no(*a, **k):
        return False

    monkeypatch.setattr(term, "_tmux_has_session", no)
    with pytest.raises(RuntimeError, match="no live tmux session 'member-alice' to watch"):
        await term._build_command(
            "attach", "jira", "PROJ-1", "s1", "card1", None, None, None,
            watch={"session": "member-alice", "socket": "agents", "host": None},
        )


@pytest.mark.asyncio
async def test_build_command_attach_watch_remote_host_wraps_ssh(roam, monkeypatch):
    """watch.host is resolved independently of the request's own `host` (which the API
    always forces to None for kind=attach) — a remote watch target still gets wrapped
    in ssh, tmux resolved via the remote's own PATH."""
    async def yes(*a, **k):
        return True

    monkeypatch.setattr(term, "_tmux_has_session", yes)
    cmd, writable, name, cwd, sid = await term._build_command(
        "attach", "jira", "PROJ-1", "s1", "card1", None, None, None,
        watch={"session": "member-alice", "socket": "agents", "host": "roam"},
    )
    assert cmd[0] == "ssh" and "mbp.tail.ts.net" in cmd
    inner = cmd[-1]
    # unconfigured named socket → the read-only ceiling applies here too, and the
    # -r/read-only flags must survive INTO the ssh-wrapped inner command
    assert "tmux -L agents attach -t member-alice -r -f read-only,ignore-size" in inner
    assert writable is False and name == "member-alice"


@pytest.mark.asyncio
async def test_open_raw_tmux_readonly_attach_argv_is_fully_hardened(monkeypatch):
    """Pins the SECOND attach path: open_raw_tmux on an unconfigured named socket must
    force read-only AND carry the full `-r -f read-only,ignore-size` hardening —
    _build_command's watch path and this one drifted apart once (a bare -r here let the
    viewer join window-size negotiation and reflow the watched pane)."""
    captured = {}

    class _P:
        pid = 1
        returncode = None

    async def fake_exec(*args, **kw):
        captured["args"] = list(args)
        return _P()

    async def noop_style(*a, **k):
        return None

    monkeypatch.setattr(term.settings, "extra_tmux_sockets", "")
    monkeypatch.setattr(term, "_free_port", lambda start: 17501)
    monkeypatch.setattr(term.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(term, "apply_tmux_style", noop_style)

    out = await term.open_raw_tmux("member-x", writable=True, socket="agents")
    args = captured["args"]
    tail = args[args.index("tmux"):]
    assert tail == ["tmux", "-L", "agents", "attach", "-r", "-f", "read-only,ignore-size", "-t", "member-x"]
    assert "--writable" not in args  # the ttyd half of the double lock
    term._SESSIONS.pop(out["id"], None)  # don't leak into other tests


@pytest.mark.asyncio
async def test_tmux_has_session_uses_named_socket(monkeypatch):
    seen = {}

    async def fake_run(argv, host=None, **kw):
        seen["argv"] = argv
        return 0, b""

    monkeypatch.setattr(term, "_run", fake_run)
    assert await term._tmux_has_session("member-alice", socket="agents") is True
    assert seen["argv"] == [term.settings.tmux_bin, "-L", "agents", "has-session", "-t", "member-alice"]


@pytest.mark.asyncio
async def test_tmux_has_session_default_socket_unchanged(monkeypatch):
    """Regression: no socket arg (every EXISTING call site) keeps the exact same argv."""
    seen = {}

    async def fake_run(argv, host=None, **kw):
        seen["argv"] = argv
        return 0, b""

    monkeypatch.setattr(term, "_run", fake_run)
    assert await term._tmux_has_session("conductor-abcd1234") is True
    assert seen["argv"] == [term.settings.tmux_bin, "has-session", "-t", "conductor-abcd1234"]


BG_MONITOR_FRAME = (
    "✻ Cogitated for 20m 3s · 2 shells still running\n"
    "❯\n"
    "  fms feat/x ~1 Opus ctx:40%\n"
    "  ⏵⏵ auto mode on · PR #19871 · 2 shells · ← for agents"
)
BG_CMD_FRAME = (
    '⏺ Background command "Watch PR 113 CI checks\nuntil completion"\n'
    "✻ Brewed for 3m 23s · 1 shell still running\n"
    "❯\n  auto mode on"
)


@pytest.mark.asyncio
async def test_classify_background_monitor_is_running(monkeypatch):
    """A claude that parked a CI watcher shows an idle prompt but 'N shells still
    running' — that's autonomously in flight (AI Working), NOT ball-in-court."""
    term._PANE_SEEN.clear()
    _fake_pane(monkeypatch, [BG_MONITOR_FRAME])
    assert await term._classify_pane("s", None) == ("running", "2 shells running", None)


@pytest.mark.asyncio
async def test_classify_background_command_label_unwraps(monkeypatch):
    """The watcher's own description (line-wrapped by the pane) becomes the card
    label, whitespace collapsed."""
    term._PANE_SEEN.clear()
    _fake_pane(monkeypatch, [BG_CMD_FRAME])
    state, bg, _choice = await term._classify_pane("s", None)
    assert state == "running"
    assert bg == "Watch PR 113 CI checks until completion"
