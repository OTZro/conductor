"""M3 batch-1 contract tests: the five Spec surfaces on the kernel.

PollSpec / NotifySpec / LinkEnricherSpec / StateProbeSpec / HookSpec now live in
kernel SpecRegistries (``conductor.specs.*``); the ``Plugin`` dataclass stays the
authoring format (discovery registers it — that IS the compat shim). Proven here:

(a) ``register_plugin`` carries all five surfaces in the module's kernel scope and
    ``dispose_plugin`` withdraws them precisely;
(b) every core consumption site reads the kernel surface: a registrant with NO
    ``Plugin`` dataclass at all — registered straight into a SpecRegistry — is
    consumed by polls / notify / enrich / probes / hooks / auth exemption alike
    (the new capability), and disposing its Effect withdraws it;
(c) for discovery-authored plugins the live ``PLUGINS`` entry stays authoritative
    (read-through), which is what keeps dict-injection (the older test idiom)
    behaviorally identical.

Behavioral identity of the five surfaces themselves is pinned by the existing
suite (test_extensibility / test_plugin_hooks / test_state_probe /
test_link_enrichers), which this milestone must not touch.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import conductor.main as main_mod
import conductor.plugins as plugins_pkg
from conductor import db as db_mod
from conductor import enrich, notify, store
from conductor import lanes as lanes_mod
from conductor.actions import agent_state
from conductor.actions import terminal as term_actions
from conductor.auth import middleware as mw
from conductor.config import settings
from conductor.main import app
from conductor.plugins import (
    CardWidgetSpec,
    HookSpec,
    LaneChangeSpec,
    LinkEnricherSpec,
    NotifySpec,
    Plugin,
    PollSpec,
    StageSpec,
    StateProbeSpec,
    TabSpec,
    runtime,
)
from conductor.plugins.runtime import SPEC_FIELDS, SlotRegistration, SpecRegistration


async def _tick():  # pragma: no cover — poll body never runs in these tests
    return None


async def _probe(ctx):
    return ("running", "kernel says")


async def _fetch(ref):
    return {"title": f"meta for {ref}"}


async def _channel(ctx):
    return None


def _full_plugin(pid: str = "zz-specs") -> Plugin:
    return Plugin(
        id=pid, label="Specs", icon="🧪",
        hooks=(HookSpec(event="PostToolUse", path=f"/api/plugins/{pid}/hook"),),
        state_probes=(StateProbeSpec(probe=_probe),),
        polls=(PollSpec(name="tick", fn=_tick, interval=5),),
        link_enrichers=(LinkEnricherSpec(kind=f"{pid}-kind", fetch=_fetch),),
        notifiers=(NotifySpec(callback=_channel),),
    )


@pytest.fixture
def kernel_only():
    """Register kernel-native rows (no Plugin dataclass anywhere) and guarantee
    they are withdrawn afterwards, whatever the test did."""
    effects = []

    def _register(field: str, *specs) -> SpecRegistration:
        reg = SpecRegistration(plugin_id="zz-kern", field=field, specs=tuple(specs))
        effects.append(runtime.spec_registry(field).register(reg))
        return reg

    yield _register
    for effect in effects:
        effect.dispose()


# ── (a) discovery scope carries the five surfaces; dispose is precise ────────


def test_register_plugin_scopes_all_five_surfaces():
    plugin = _full_plugin()
    scope = runtime.register_plugin("zz-specs-mod", plugin)
    try:
        for field in SPEC_FIELDS:
            reg = runtime.spec_registry(field).get("zz-specs")
            assert reg is not None, f"{field} declaration missing from kernel"
            assert reg.field == field
            assert reg.specs == tuple(getattr(plugin, field))
    finally:
        runtime.dispose_plugin("zz-specs-mod")
    assert scope.disposed
    for field in SPEC_FIELDS:
        assert runtime.spec_registry(field).get("zz-specs") is None


def test_dispose_removes_only_that_plugins_rows():
    a, b = _full_plugin("zz-a"), _full_plugin("zz-b")
    runtime.register_plugin("zz-a-mod", a)
    runtime.register_plugin("zz-b-mod", b)
    try:
        runtime.dispose_plugin("zz-a-mod")
        for field in SPEC_FIELDS:
            assert runtime.spec_registry(field).get("zz-a") is None
            assert runtime.spec_registry(field).get("zz-b") is not None
    finally:
        runtime.dispose_plugin("zz-a-mod")
        runtime.dispose_plugin("zz-b-mod")


# ── (b) consumption reads the kernel: dataclass-less registrants are served ──


def test_kernel_only_poll_is_enumerated_and_disposed(monkeypatch, kernel_only):
    kernel_only("polls", PollSpec(name="tick", fn=_tick, interval=5))
    monkeypatch.setattr(main_mod, "_PLUGIN_POLLS", None)
    assert ("zz-kern.tick", _tick, 5.0) in main_mod._plugin_polls()

    # a second registrant proves withdrawal is visible to the CONSUMER, not just
    # the registry: dispose its Effect and re-enumerate.
    eff = runtime.spec_registry("polls").register(
        SpecRegistration(plugin_id="zz-kern2", field="polls",
                         specs=(PollSpec(name="tock", fn=_tick, interval=7),))
    )
    main_mod._PLUGIN_POLLS = None
    assert ("zz-kern2.tock", _tick, 7.0) in main_mod._plugin_polls()
    eff.dispose()
    main_mod._PLUGIN_POLLS = None
    names = [n for n, _f, _i in main_mod._plugin_polls()]
    assert "zz-kern2.tock" not in names and "zz-kern.tick" in names


async def test_kernel_only_notifier_gets_the_fan_out(kernel_only):
    seen: list[dict] = []

    async def channel(ctx):
        seen.append(ctx)

    kernel_only("notifiers", NotifySpec(callback=channel))
    notify._fan_out({"event": "need_human", "title": "T", "subtitle": "", "ref": "R", "link": None})
    await asyncio.sleep(0.05)
    assert seen and seen[0]["title"] == "T"


def test_kernel_only_link_enricher_is_in_specs(kernel_only):
    spec = LinkEnricherSpec(kind="zz-kern-kind", fetch=_fetch)
    kernel_only("link_enrichers", spec)
    assert enrich._specs().get("zz-kern-kind") is spec


async def test_kernel_only_state_probe_promotes(kernel_only):
    kernel_only("state_probes", StateProbeSpec(probe=_probe))
    got = await agent_state._probe_state("card-k", {("s1", None)}, False)
    assert got == ("running", "kernel says")


def test_kernel_only_hook_reaches_settings_and_auth_exemption(monkeypatch, kernel_only):
    kernel_only("hooks", HookSpec(event="PostToolUse", path="/api/plugins/zzkern/capture", matcher="SendUserFile"))
    rows = list(term_actions._plugin_hooks("http://x", "card-1", None, ""))
    mine = [(ev, e) for ev, e in rows if "/api/plugins/zzkern/capture" in e["hooks"][0]["command"]]
    assert len(mine) == 1
    event, entry = mine[0]
    assert event == "PostToolUse" and entry["matcher"] == "SendUserFile"
    assert "card_id=card-1" in entry["hooks"][0]["command"]

    monkeypatch.setattr(mw, "_plugin_hook_paths", None)  # force a fresh compute
    assert "/api/plugins/zzkern/capture" in mw._hook_paths()


# ── (c) read-through: the live PLUGINS entry stays authoritative ─────────────


def test_discovery_rows_read_through_to_live_plugins_dict(monkeypatch):
    import conductor.plugins as plugins_pkg

    runtime.register_plugin("zz-rt-mod", _full_plugin("zz-rt"))
    try:
        replaced = Plugin(id="zz-rt", label="R", icon="🧪",
                          polls=(PollSpec(name="other", fn=_tick, interval=9),))
        monkeypatch.setitem(plugins_pkg.PLUGINS, "zz-rt", replaced)
        rows = dict(runtime.spec_rows("polls"))
        assert rows["zz-rt"].name == "other"  # dict entry, not the boot snapshot, wins
    finally:
        runtime.dispose_plugin("zz-rt-mod")


# ── M3 batch 2: stages / lane_changes / card_body / slot provider calls ──────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/kernel-specs-test.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c
    await engine.dispose()


async def _b2_cb(ctx):  # pragma: no cover — callback body never runs here
    return None


async def _b2_body(ctx):  # pragma: no cover — provider body never runs here
    return None


def _batch2_plugin(pid: str = "zz-b2") -> Plugin:
    return Plugin(
        id=pid, label="B2", icon="🧪",
        stages=(StageSpec(key=f"{pid}-stage", title="B2", order=7),),
        lane_changes=(LaneChangeSpec(callback=_b2_cb),),
        card_body=_b2_body,
    )


def test_register_plugin_scopes_batch2_surfaces():
    """stages / lane_changes ride SPEC_FIELDS; card_body (scalar) is normalized to a
    1-tuple. dispose_plugin withdraws all three."""
    plugin = _batch2_plugin()
    scope = runtime.register_plugin("zz-b2-mod", plugin)
    try:
        for field in ("stages", "lane_changes"):
            reg = runtime.spec_registry(field).get("zz-b2")
            assert reg is not None, f"{field} declaration missing from kernel"
            assert reg.specs == tuple(getattr(plugin, field))
        body = runtime.spec_registry("card_body").get("zz-b2")
        assert body is not None and body.specs == (plugin.card_body,)
    finally:
        runtime.dispose_plugin("zz-b2-mod")
    assert scope.disposed
    for field in ("stages", "lane_changes", "card_body"):
        assert runtime.spec_registry(field).get("zz-b2") is None


def test_kernel_only_stage_reaches_stage_registry(tmp_path, monkeypatch, kernel_only):
    monkeypatch.setattr(settings, "lanes_file", tmp_path / "lanes.json")  # absent → none
    monkeypatch.setattr(lanes_mod, "_config_stages", lanes_mod._ConfigStages())
    kernel_only("stages", StageSpec(key="zz-kern-stage", title="Kern", order=6))
    assert "zz-kern-stage" in [lane["key"] for lane in lanes_mod.stage_registry()]

    # withdrawal is visible to the CONSUMER: a second registrant, disposed, vanishes
    eff = runtime.spec_registry("stages").register(
        SpecRegistration(plugin_id="zz-kern2", field="stages",
                         specs=(StageSpec(key="zz-kern-stage2", title="K2", order=6),))
    )
    assert "zz-kern-stage2" in [lane["key"] for lane in lanes_mod.stage_registry()]
    eff.dispose()
    keys = [lane["key"] for lane in lanes_mod.stage_registry()]
    assert "zz-kern-stage2" not in keys and "zz-kern-stage" in keys


async def test_kernel_only_lane_change_fires_and_errors_stay_swallowed(kernel_only):
    seen: list[dict] = []

    async def boom(ctx):
        raise RuntimeError("plugin bug")

    async def cb(ctx):
        seen.append(ctx)

    # broken callback FIRST — the good one must still fire and nothing may raise
    kernel_only("lane_changes", LaneChangeSpec(callback=boom), LaneChangeSpec(callback=cb))
    store.emit_lane_change("c1", "zz", "Z-1", "need_human", "pending", "human")
    await asyncio.sleep(0.05)
    assert seen and seen[0]["card_id"] == "c1" and seen[0]["new_lane"] == "pending"


async def test_kernel_only_card_body_provider_serves_endpoint(client, kernel_only):
    async def body(ctx):
        return {"kind": "zz-kern", "content": f"kernel body for {ctx['external_id']}"}

    kernel_only("card_body", body)
    cid = (await client.post("/api/cards", json={"title": "b2"})).json()["id"]
    got = (await client.get(f"/api/cards/{cid}/body")).json()
    assert got["kind"] == "zz-kern" and got["content"].startswith("kernel body for")


# ── slot provider calls resolve via the kernel ───────────────────────────────


async def test_kernel_native_tab_and_card_are_callable(client):
    """A registrant with rows only in the slot registries (no PLUGINS entry) is
    served by GET /{id}/tab and POST /{id}/card; disposal returns them to 404."""

    async def tab_data():
        return {"rows": [1, 2]}

    async def card_data(ctx):
        return {"got": ctx.get("external_id")}

    effects = [
        runtime.slot_registry("tab").register(SlotRegistration(
            plugin_id="zz-kern-slot", slot="tab",
            payload={"layout": "zz", "self_contained": False, "config": {}},
            spec=TabSpec(layout="zz", provider=tab_data),
        )),
        runtime.slot_registry("card_widget").register(SlotRegistration(
            plugin_id="zz-kern-slot", slot="card_widget",
            payload={"title": "T", "layout": "zz", "slot": "body"},
            spec=CardWidgetSpec(title="T", layout="zz", provider=card_data),
        )),
    ]
    try:
        r = await client.get("/api/plugins/zz-kern-slot/tab")
        assert r.status_code == 200 and r.json() == {"rows": [1, 2]}
        r = await client.post("/api/plugins/zz-kern-slot/card", json={"external_id": "X-9"})
        assert r.status_code == 200 and r.json() == {"data": {"got": "X-9"}}
    finally:
        for eff in effects:
            eff.dispose()
    assert (await client.get("/api/plugins/zz-kern-slot/tab")).status_code == 404
    assert (await client.post("/api/plugins/zz-kern-slot/card", json={})).status_code == 404


async def test_plugins_entry_without_kernel_row_still_serves_tab_and_card(client, monkeypatch):
    """The failed-registration degrade path (M3a philosophy): a plugin live in
    PLUGINS but absent from the kernel — dict injection, or discovery's
    kernel_registered=False — keeps serving through the read-through. Degrade to
    legacy, never vanish."""

    async def tab_data():
        return {"legacy": True}

    async def card_data(ctx):
        return "legacy-widget"

    legacy = Plugin(
        id="zz-legacy", label="L", icon="🧪",
        tab=TabSpec(layout="zz", provider=tab_data),
        card_widget=CardWidgetSpec(title="T", layout="zz", provider=card_data),
    )
    monkeypatch.setitem(plugins_pkg.PLUGINS, "zz-legacy", legacy)
    assert runtime.slot_registry("tab").get("zz-legacy") is None  # truly no kernel row
    assert (await client.get("/api/plugins/zz-legacy/tab")).json() == {"legacy": True}
    r = await client.post("/api/plugins/zz-legacy/card", json={})
    assert r.json() == {"data": "legacy-widget"}


def test_failed_mirror_plugin_keeps_discovery_order(monkeypatch):
    """M3b verifier repro: discovery loaded B then A, but B's kernel mirror
    registration failed (_scan sets PLUGINS before the register_plugin try, so
    PLUGINS has B and the kernel does not). spec_rows must still yield B's rows
    before A's — the old kernel-first iteration pushed B to the TAIL, flipping
    first-wins collisions (stage keys, card_body)."""
    b = Plugin(id="zz-ord-b", label="B", icon="🧪",
               stages=(StageSpec(key="zz-ord-b-stage", title="B", order=6),))
    a = Plugin(id="zz-ord-a", label="A", icon="🧪",
               stages=(StageSpec(key="zz-ord-a-stage", title="A", order=6),))
    monkeypatch.setitem(plugins_pkg.PLUGINS, "zz-ord-b", b)  # discovery order: B first
    monkeypatch.setitem(plugins_pkg.PLUGINS, "zz-ord-a", a)
    runtime.register_plugin("zz-ord-a-mod", a)  # A mirrored fine; B's mirror "failed"
    try:
        ids = [pid for pid, _ in runtime.spec_rows("stages") if pid.startswith("zz-ord-")]
        assert ids == ["zz-ord-b", "zz-ord-a"]
    finally:
        runtime.dispose_plugin("zz-ord-a-mod")
