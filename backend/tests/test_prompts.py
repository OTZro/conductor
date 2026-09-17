"""Prompt overrides (local > shipped defaults), {base} composition, last-known-good on
a corrupt file, unknown-key warnings, and the config typo guard."""

import json
import os

import pytest

from conductor import prompts
from conductor.config import settings, warn_unknown_keys


@pytest.fixture(autouse=True)
def local_file(monkeypatch, tmp_path):
    """Fresh mtime cache per test; the local prompts file points into tmp (absent by
    default = no overrides)."""
    monkeypatch.setattr(prompts, "_local", prompts._Layer("local"))
    local = tmp_path / "prompts.json"
    monkeypatch.setattr(settings, "prompts_file", local)
    return local


def _bump_mtime(path):
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 2))


def test_default_when_no_files():
    assert prompts.prompt("handover_write", cid="c1") == prompts.DEFAULTS["handover_write"].format(cid="c1")


def test_local_override_wins(local_file):
    local_file.write_text(json.dumps({"seed_slack_closing": "local wins"}))
    assert prompts.prompt("seed_slack_closing") == "local wins"


def test_base_embeds_the_default(local_file):
    # {base} wraps the shipped default; placeholders fill in the same final pass
    local_file.write_text(json.dumps({"seed_slack_closing": "PREFIX. {base}"}))
    assert prompts.prompt("seed_slack_closing") == "PREFIX. " + prompts.DEFAULTS["seed_slack_closing"]
    local_file.write_text(json.dumps({"seed_slack_link": "wrapped[{base}]"}))
    _bump_mtime(local_file)
    expected = ("wrapped[" + prompts.DEFAULTS["seed_slack_link"] + "]").format(url="U")
    assert prompts.prompt("seed_slack_link", url="U") == expected


def test_corrupt_file_keeps_last_known_good(local_file, caplog):
    local = local_file
    local.write_text(json.dumps({"seed_slack_closing": "good version"}))
    assert prompts.prompt("seed_slack_closing") == "good version"
    local.write_text("{not json")
    _bump_mtime(local)
    with caplog.at_level("WARNING"):
        assert prompts.prompt("seed_slack_closing") == "good version"  # NOT the default
    assert any("last good version" in r.message for r in caplog.records)
    # warned once per bad mtime, not per call
    caplog.clear()
    with caplog.at_level("WARNING"):
        prompts.prompt("seed_slack_closing")
    assert not caplog.records


def test_deleted_file_drops_the_overrides(local_file):
    local_file.write_text(json.dumps({"seed_slack_closing": "temp"}))
    assert prompts.prompt("seed_slack_closing") == "temp"
    local_file.unlink()
    assert prompts.prompt("seed_slack_closing") == prompts.DEFAULTS["seed_slack_closing"]


def test_unknown_key_warned_and_ignored(local_file, caplog):
    local = local_file
    local.write_text(json.dumps({"not_a_prompt": "x", "seed_slack_closing": "ok"}))
    with caplog.at_level("WARNING"):
        assert prompts.prompt("seed_slack_closing") == "ok"
    assert any("unknown key 'not_a_prompt'" in r.message for r in caplog.records)


def test_broken_placeholder_falls_back_to_default(local_file, caplog):
    local = local_file
    local.write_text(json.dumps({"handover_write": "broken {nope}"}))
    with caplog.at_level("WARNING"):
        out = prompts.prompt("handover_write", cid="c9")
    assert out == prompts.DEFAULTS["handover_write"].format(cid="c9")
    assert any("failed to format" in r.message for r in caplog.records)


def test_every_default_key_formats():
    kw = {
        "slack_brief_thread": {"who": " (K)"}, "slack_brief_single": {"who": ""},
        "pr_brief": {"title": "t", "body": "b", "review": "r"},
        "seed_slack_mention": {"text": "x"}, "seed_slack_link": {"url": "u"},
        "seed_slack_gist": {"brief": "g"}, "seed_slack_closing": {},
        "handover_write": {"cid": "c"}, "handover_pickup": {"cid": "c"},
    }
    assert set(kw) == set(prompts.DEFAULTS)
    for k, a in kw.items():
        assert prompts.prompt(k, **a)


# ── config typo guard ────────────────────────────────────────────────────────────


def test_warn_unknown_keys_flags_typos(monkeypatch, caplog):
    monkeypatch.setenv("CONDUCTOR_JIRA_JQLL", "oops")  # typo'd var
    monkeypatch.setenv("CONDUCTOR_PORT", "8787")  # valid — must not warn
    with caplog.at_level("WARNING"):
        warn_unknown_keys()
    msgs = [r.message for r in caplog.records]
    assert any("CONDUCTOR_JIRA_JQLL" in m for m in msgs)
    assert not any("CONDUCTOR_PORT" in m for m in msgs)
