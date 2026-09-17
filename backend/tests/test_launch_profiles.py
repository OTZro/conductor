"""Unit tests for named launch profiles (~/.conductor/profiles.json)."""

import json

import pytest

from conductor import launch_profiles as lp


@pytest.fixture
def profiles_file(monkeypatch, tmp_path):
    p = tmp_path / "profiles.json"
    monkeypatch.setattr(lp.settings, "launch_profiles_file", p)
    return p


def test_missing_file_is_no_profiles(profiles_file):
    # absent file → feature simply off, never an error
    assert lp.resolve("anything") is None
    assert lp.summaries() == []


def test_corrupt_file_is_ignored(profiles_file):
    profiles_file.write_text("{ not json")
    assert lp.resolve("x") is None
    assert lp.summaries() == []


def test_non_object_file_is_ignored(profiles_file):
    profiles_file.write_text(json.dumps(["not", "a", "map"]))
    assert lp.summaries() == []
    assert lp.resolve("x") is None


def test_resolve_normalizes_spec(profiles_file):
    profiles_file.write_text(
        json.dumps(
            {
                "review": {
                    "host": "base",
                    "cwd": "~/code",
                    "env": {"CLAUDE_CONFIG_DIR": "~/.claude-review"},
                },
                "bare": {},
            }
        )
    )
    assert lp.resolve("review") == {
        "host": "base",
        "cwd": "~/code",
        "env": {"CLAUDE_CONFIG_DIR": "~/.claude-review"},
    }
    # an empty spec → all-None/empty, not a crash
    assert lp.resolve("bare") == {"host": None, "cwd": None, "env": {}}
    assert lp.resolve("unknown") is None


def test_summaries_include_env_for_the_tooltip(profiles_file):
    profiles_file.write_text(
        json.dumps({"review": {"host": "roam", "cwd": "~/code", "env": {"CLAUDE_CONFIG_DIR": "~/.x"}}})
    )
    assert lp.summaries() == [
        {"name": "review", "host": "roam", "cwd": "~/code", "env": {"CLAUDE_CONFIG_DIR": "~/.x"}}
    ]
