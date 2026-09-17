"""Unit tests for the Terminals-sidebar pane status parser (pure, no tmux)."""

from conductor.actions.agent_state import (
    _clean_summary,
    _looks_like_claude,
    _pane_status,
)

# a realistic idle pane: prompt box + my statusline at the bottom
IDLE = """\
● Done. Anything else?

╭─────────────────────────────────────╮
│ >                                   │
╰─────────────────────────────────────╯
  frontend master ~6 Opus 4.8 ctx:77% session:3% "在 header 加 usage"
  ⏵⏵ auto mode on (shift+tab to cycle)
"""

RUNNING = """\
✻ Boogieing… (3m 45s · ↓ 2.0k tokens · esc to interrupt)
  fms feat/PROJ-7116 ~2 Fable 5 ctx:16% session:44%
"""

BG = """\
  ◯ 2 shells still running
❯
  svc-rocket main Sonnet 4.6 ctx:30%
"""

# the INITIAL thinking phase (live-captured 2026-07-16): no token counter, no
# "esc to interrupt" yet — just the spinner-glyph gerund line. Must classify as
# running, or a fresh prompt sits in Need Human until the first tool call.
THINKING_BARE = """\
✳ Wibbling…
  conductor master ~3 Opus 4.8
  ⏵⏵ auto mode on (shift+tab to cycle)
"""

THINKING_TIMER = """\
✳ Caramelizing… (2s · thinking)
  conductor master ~3 Opus 4.8 ctx:5% session:11%
  ⏵⏵ auto mode on (shift+tab to cycle)
"""


# a live AskUserQuestion dialog (live-captured 2026-07-16) — claude is waiting for
# the human to PICK; must surface the question as `choice` (drives the amber banner)
QUESTION_MODAL = """\
❯ Use your AskUserQuestion tool to ask me: coffee or tea?
────────────────────────────────────────
 ☐ Drink
Coffee or tea?
❯ 1. Coffee
     A cup of coffee.
  2. Tea
     A cup of tea.
  3. Type something.
────────────────────────────────────────
  4. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
"""

# a permission dialog has the same cursor-on-numbered-options chrome
PERMISSION_DIALOG = """\
  Bash command
  echo probe
  Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again for echo commands
  3. No, and tell Claude what to do differently (esc)
"""


def test_question_modal_surfaces_choice():
    d = _pane_status(QUESTION_MODAL)
    assert d["state"] == "waiting"
    assert d["choice"] == "Coffee or tea?"


def test_permission_dialog_surfaces_choice():
    d = _pane_status(PERMISSION_DIALOG)
    assert d["state"] == "waiting"
    assert d["choice"] == "Do you want to proceed?"


def test_idle_and_running_have_no_choice():
    assert _pane_status(IDLE)["choice"] is None
    assert _pane_status(RUNNING)["choice"] is None


def test_initial_thinking_is_running():
    assert _pane_status(THINKING_BARE)["state"] == "running"
    assert _pane_status(THINKING_TIMER)["state"] == "running"


def test_idle_pane_parses_statusline():
    d = _pane_status(IDLE)
    assert d["state"] == "waiting"
    assert d["bg"] is None
    assert d["model"] == "Opus 4.8"
    assert d["ctx_pct"] == 77
    assert d["task"] == "在 header 加 usage"


def test_running_spinner():
    d = _pane_status(RUNNING)
    assert d["state"] == "running"
    assert d["model"] == "Fable 5"
    assert d["ctx_pct"] == 16
    assert d["task"] is None  # no quoted prompt on the line


def test_background_task_counts_as_running():
    d = _pane_status(BG)
    assert d["state"] == "running"
    assert d["bg"] == "2 shells running"
    assert d["model"] == "Sonnet 4.6"
    assert d["ctx_pct"] == 30


def test_no_statusline_degrades():
    d = _pane_status("just some shell output\n$ ls\n")
    assert d["state"] == "waiting"
    assert d["model"] is None and d["ctx_pct"] is None and d["task"] is None


def test_clean_summary_strips_claude_glyphs():
    assert _clean_summary("✳ 修復 ALL-office 模板編輯權限錯誤") == "修復 ALL-office 模板編輯權限錯誤"
    assert _clean_summary("⠂ 設計 scheduler flow") == "設計 scheduler flow"  # braille spinner
    assert _clean_summary("  ✻  Analyzing tickets") == "Analyzing tickets"


def test_clean_summary_drops_placeholder_and_empty():
    assert _clean_summary("✳ Claude Code") is None  # default title, not a real summary
    assert _clean_summary("Claude Code") is None
    assert _clean_summary("   ") is None
    assert _clean_summary("") is None


# --- _looks_like_claude: gate for surfacing status on card-less 'other' sessions ---


def test_looks_like_claude_by_statusline():
    # idle claude, no spinner — recognized purely by its ctx:/model statusline
    assert _looks_like_claude(IDLE, _pane_status(IDLE)) is True


def test_looks_like_claude_by_spinner():
    assert _looks_like_claude(RUNNING, _pane_status(RUNNING)) is True


def test_looks_like_claude_by_chrome_without_statusline():
    # no statusline (model/ctx None, not running) but claude's own UI chrome is on screen
    pane = "some output\n╰──────╯\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    st = _pane_status(pane)
    assert st["model"] is None and st["ctx_pct"] is None and st["state"] == "waiting"
    assert _looks_like_claude(pane, st) is True


def test_plain_shell_is_not_claude():
    pane = "just some shell output\n$ ls\nfile.txt  dir/\n$ "
    assert _looks_like_claude(pane, _pane_status(pane)) is False
