"""api/themes.py — a user theme is untrusted input that reaches CSS, ttyd's argv and a
tmux set-option, so every field is allowlisted. These lock that down."""

from __future__ import annotations

from conductor.api.themes import parse_theme


def test_minimal_theme_defaults_to_dark():
    t = parse_theme({"id": "nord", "label": "Nord"})
    assert t == {
        "id": "nord", "label": "Nord", "base": "dark",
        "vars": {}, "terminal": {}, "tmux": {}, "source": "file",
    }


def test_id_and_label_are_required():
    assert parse_theme({"label": "no id"}) is None
    assert parse_theme({"id": "no-label"}) is None
    assert parse_theme({"id": "  ", "label": "blank"}) is None
    assert parse_theme("not a dict") is None


def test_unknown_base_falls_back_to_dark():
    assert parse_theme({"id": "x", "label": "x", "base": "solarized"})["base"] == "dark"
    assert parse_theme({"id": "x", "label": "x", "base": "light"})["base"] == "light"


def test_vars_must_be_rgb_channels_not_hex():
    """`rgb(var(--x) / <alpha-value>)` needs bare channels; a hex would silently break
    every opacity modifier in the app rather than fail loudly."""
    t = parse_theme({"id": "x", "label": "x", "vars": {
        "--z-950": "46 52 64",    # kept
        "--a-sky": "#38bdf8",     # dropped: hex, not channels
        "notavar": "1 2 3",       # dropped: not a custom property
        "--z-900": "oops",        # dropped
    }})
    assert t["vars"] == {"--z-950": "46 52 64"}


def test_terminal_palette_must_be_hex():
    t = parse_theme({"id": "x", "label": "x", "terminal": {
        "background": "#2e3440", "foreground": "#fff", "red": "rgb(1,2,3)",
    }})
    assert t["terminal"] == {"background": "#2e3440", "foreground": "#fff"}


def test_tmux_options_and_values_are_allowlisted():
    """These end up in a `tmux set-option` argv — an unknown option or a value outside
    the style charset is dropped, not forwarded."""
    t = parse_theme({"id": "x", "label": "x", "tmux": {
        "status-style": "fg=#d8dee9,bg=#3b4252",   # kept
        "pane-border-style": "fg=#4c566a",         # kept
        "default-command": "/bin/sh",              # dropped: not a style option
        "status-left": "#(whoami)",                # dropped: not in the allowlist
        "message-style": "fg=#fff; rm -rf /",      # dropped: illegal characters
    }})
    assert t["tmux"] == {
        "status-style": "fg=#d8dee9,bg=#3b4252",
        "pane-border-style": "fg=#4c566a",
    }


def test_non_string_values_are_dropped():
    t = parse_theme({"id": "x", "label": "x", "vars": {"--z-950": 12}, "tmux": {"status-style": None}})
    assert t["vars"] == {} and t["tmux"] == {}
