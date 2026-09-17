"""The generic plugin-hook framework: a plugin declares a ``HookSpec`` and core folds it
into every session's generated claude-hook settings AND auto-exempts its endpoint from
the auth gate — with no core edit per plugin. These tests assert that genericity over
whatever plugins are installed (not hardcoding the files plugin), plus a synthetic plugin
to prove a brand-new hook nobody special-cased still wires end to end."""

from __future__ import annotations

from conductor.actions.terminal import _hook_cfg
from conductor.auth import middleware as mw
from conductor.plugins import PLUGINS, HookSpec, Plugin


def _all_specs():
    return [(p, s) for p in PLUGINS.values() for s in p.hooks]


def _entry_for(cfg: dict, spec: HookSpec) -> dict:
    """The entry wired for THIS spec — selected by its PATH, not by matcher.

    Matcher alone is not a selector: core's own built-in state hooks carry no matcher
    either, so a spec with matcher=None used to pick whichever entry happened to be
    first, and that was core's. It only looked right while no plugin declared a hook on
    an event core also uses (UserPromptSubmit / Pre|PostToolUse / SessionEnd)."""
    entries = cfg["hooks"].get(spec.event) or []
    matching = [
        e
        for e in entries
        if e.get("matcher") == spec.matcher
        and any(spec.path in h.get("command", "") for h in (e.get("hooks") or []))
    ]
    assert matching, f"hook {spec.event}/{spec.matcher} → {spec.path} not wired"
    return matching[0]


def test_every_declared_plugin_hook_is_wired_into_settings():
    cfg = _hook_cfg("card-x", "http://127.0.0.1:8787", host=None)
    specs = _all_specs()
    assert specs, "expected at least one plugin (files) to declare a hook"
    for _plugin, spec in specs:
        cmd = _entry_for(cfg, spec)["hooks"][0]["command"]
        assert spec.path in cmd and "card_id=card-x" in cmd


def test_built_in_state_hooks_survive_alongside_plugin_hooks():
    # folding plugin hooks in must not drop core's own working/idle/notify wiring
    cfg = _hook_cfg("c1", "http://127.0.0.1:8787", host=None)
    assert any("/agent" in h["command"] for h in cfg["hooks"]["PostToolUse"][0]["hooks"])
    assert any("/notify" in h["command"] for h in cfg["hooks"]["Notification"][0]["hooks"])
    assert any("/agent" in h["command"] for h in cfg["hooks"]["SessionEnd"][0]["hooks"])


def test_host_is_threaded_to_plugin_hooks():
    remote = _hook_cfg("c1", "https://pub", host="roam")
    for _plugin, spec in _all_specs():
        assert "host=roam" in _entry_for(remote, spec)["hooks"][0]["command"]
    local = _hook_cfg("c1", "http://127.0.0.1:8787", host=None)
    for _plugin, spec in _all_specs():
        assert "host=" not in _entry_for(local, spec)["hooks"][0]["command"]


def test_every_plugin_hook_path_is_auth_exempt(monkeypatch):
    monkeypatch.setattr(mw, "_plugin_hook_paths", None)  # force a fresh compute
    exempt = mw._hook_paths()
    for _plugin, spec in _all_specs():
        assert spec.path in exempt


def test_a_new_plugin_hook_wires_with_no_core_edit(monkeypatch):
    """The whole point: a plugin nobody special-cased, added at runtime, is wired into
    the settings and exempted by the gate purely from its HookSpec."""
    fake = Plugin(
        id="zzz-probe", label="Z", icon="🧪",
        hooks=(HookSpec(event="PreToolUse", path="/api/plugins/zzz/probe", matcher="Bash"),),
    )
    monkeypatch.setitem(PLUGINS, "zzz-probe", fake)

    cfg = _hook_cfg("cid", "http://127.0.0.1:8787", host=None)
    hit = [e for e in cfg["hooks"]["PreToolUse"] if e.get("matcher") == "Bash"]
    assert hit and "/api/plugins/zzz/probe" in hit[0]["hooks"][0]["command"]

    monkeypatch.setattr(mw, "_plugin_hook_paths", None)
    assert "/api/plugins/zzz/probe" in mw._hook_paths()
