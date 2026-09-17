"""M2 contract tests: discovery→kernel shim, zero-breakage proofs.

Proves (a) every discovery-loaded plugin owns a kernel scope whose render-slot
registrations mirror its ``Plugin`` dataclass fields byte-for-byte, (b) the
``/api/plugins/manifest`` response is exactly the pre-shim computation
(``[p.manifest() for p in PLUGINS.values()]``), (c) ``dispose_plugin`` unwinds
a scope's registrations and is idempotent, (d) disabled modules never get a
scope (nor import at all), and (e) ``Effect.dispose()`` refuses — rather than
silently swallows — an awaitable returned by a sync-looking undo.
"""

from __future__ import annotations

import sys
import textwrap

import pytest
from httpx import ASGITransport, AsyncClient

import conductor.plugins as plugins_pkg
from conductor.kernel import Effect
from conductor.main import app
from conductor.plugins import PLUGINS, RENDER_SLOTS, CardWidgetSpec, MenuBarSpec, Plugin, TabSpec
from conductor.plugins import runtime


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
def kernel_registered():
    """Re-run the discovery→kernel registration for every loaded module.

    ``reset_kernel()`` elsewhere in the suite (test_kernel.py) wipes the slot
    registries, so slot-content assertions rebuild through the SAME code path
    discovery uses (``runtime.register_plugin``) to stay order-independent.
    The re-registration is last-wins by design, so boot state is preserved."""
    for row in plugins_pkg.MODULES:
        if row["loaded"]:
            runtime.register_plugin(row["module"], PLUGINS[row["id"]])
    yield


# ── (a) every loaded plugin has a kernel scope mirroring its dataclass ───────


def test_every_loaded_module_has_a_kernel_scope():
    loaded = [r["module"] for r in plugins_pkg.MODULES if r["loaded"]]
    assert loaded, "discovery loaded nothing — test environment is broken"
    for module in loaded:
        scope = runtime.plugin_scope(module)
        assert scope is not None, f"no kernel scope for loaded module {module!r}"
        assert not scope.disposed


def test_slot_registrations_match_plugin_dataclass(kernel_registered):
    for pid, plugin in PLUGINS.items():
        manifest = plugin.manifest()
        for slot in runtime.RENDER_SLOTS:
            reg = runtime.slot_registry(slot).get(pid)
            spec = getattr(plugin, slot)
            if spec is None:
                assert reg is None, f"{pid}: kernel holds a {slot} the dataclass lacks"
            else:
                assert reg is not None, f"{pid}: {slot} declaration missing from kernel"
                assert reg.plugin_id == pid and reg.slot == slot
                assert reg.payload == manifest[slot], f"{pid}.{slot} payload drifted"
                assert reg.spec is spec


def test_kernel_covers_every_slot_declaration_exactly(kernel_registered):
    """No extra rows either: the kernel's per-slot key sets equal the set of
    plugins declaring that slot — same data, both directions."""
    for slot in runtime.RENDER_SLOTS:
        expect = {pid for pid, p in PLUGINS.items() if getattr(p, slot) is not None}
        got = {r.plugin_id for r in runtime.slot_registry(slot).registrations()}
        assert got == expect


# ── (b) manifest byte-identical to the pre-shim implementation ───────────────


async def test_manifest_identical_to_pre_shim_computation(client):
    """The endpoint body must still be exactly the ``PLUGINS``-derived
    computation — ``p.manifest()`` plus the per-slot ``orders`` graft (#36's
    plugin-manager ordering; base ``order`` everywhere absent an override),
    sorted by (order, id)."""
    r = await client.get("/api/plugins/manifest")
    assert r.status_code == 200
    expect = []
    for p in PLUGINS.values():
        m = p.manifest()
        m["orders"] = {slot: p.order for slot in RENDER_SLOTS}
        expect.append(m)
    expect.sort(key=lambda m: (m["order"], m["id"]))
    assert r.json() == expect


# ── (c) dispose_plugin: precise, idempotent teardown ─────────────────────────


async def _prov(ctx):  # pragma: no cover — provider body never runs here
    return None


def _fake_plugin(pid: str = "zz-shim") -> Plugin:
    return Plugin(
        id=pid, label="Shim", icon="🧪",
        tab=TabSpec(layout="zz-tab", config={"k": "v"}),
        card_widget=CardWidgetSpec(title="T", layout="zz-card", provider=_prov, slot="actions"),
        menu_bar=MenuBarSpec(layout="zz-menu"),
    )


def test_dispose_plugin_removes_registrations_and_is_idempotent():
    plugin = _fake_plugin()
    scope = runtime.register_plugin("zz-shim-mod", plugin)
    for slot in runtime.RENDER_SLOTS:
        assert runtime.slot_registry(slot).get("zz-shim") is not None

    runtime.dispose_plugin("zz-shim-mod")
    assert scope.disposed
    assert runtime.plugin_scope("zz-shim-mod") is None
    for slot in runtime.RENDER_SLOTS:
        assert runtime.slot_registry(slot).get("zz-shim") is None

    runtime.dispose_plugin("zz-shim-mod")  # second call: documented no-op
    assert runtime.plugin_scope("zz-shim-mod") is None


def test_reregistration_is_last_wins_and_old_scope_is_inert():
    first = runtime.register_plugin("zz-shim-mod", _fake_plugin())
    second = runtime.register_plugin("zz-shim-mod", _fake_plugin())
    try:
        assert first.disposed and not second.disposed  # replaced scope unwound
        assert runtime.plugin_scope("zz-shim-mod") is second
        # the new registrations survived the old scope's teardown
        assert runtime.slot_registry("tab").get("zz-shim") is not None
    finally:
        runtime.dispose_plugin("zz-shim-mod")


# ── (d) disabled modules: no import, no scope ────────────────────────────────


def test_scan_disabled_module_gets_no_scope(tmp_path, monkeypatch):
    pkg = tmp_path / "zzshimpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    src = textwrap.dedent(
        """
        from conductor.plugins.base import Plugin, TabSpec
        PLUGIN = Plugin(id={pid!r}, label="X", icon="x", tab=TabSpec(layout="zz"))
        """
    )
    (pkg / "zzon.py").write_text(src.format(pid="zzon"))
    (pkg / "zzoff.py").write_text(src.format(pid="zzoff"))
    (pkg / "_zzskip.py").write_text("raise RuntimeError('skip rule violated: imported')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(plugins_pkg, "MODULES", [])

    found: dict[str, Plugin] = {}
    routers: list = []
    try:
        plugins_pkg._scan(str(pkg), "zzshimpkg", found, routers, "local", {"zzoff"})

        rows = {r["module"]: r for r in plugins_pkg.MODULES}
        assert set(rows) == {"zzon", "zzoff"}  # `_`-prefixed never even rowed
        assert rows["zzoff"]["enabled"] is False and rows["zzoff"]["loaded"] is False
        assert "zzoff" not in found and f"zzshimpkg.zzoff" not in sys.modules  # never imported
        assert runtime.plugin_scope("zzoff") is None  # (d): disabled ⇒ no scope

        assert rows["zzon"]["loaded"] is True
        scope = runtime.plugin_scope("zzon")
        assert scope is not None and not scope.disposed
        assert runtime.slot_registry("tab").get("zzon") is not None
    finally:
        runtime.dispose_plugin("zzon")
        runtime.dispose_plugin("zzoff")
        for m in ("zzshimpkg", "zzshimpkg.zzon"):
            sys.modules.pop(m, None)


# ── (e) Effect.dispose must not swallow an awaitable from a sync undo ────────


def test_sync_dispose_refuses_awaitable_returning_undo():
    ran: list[bool] = []

    async def _teardown() -> None:
        ran.append(True)

    eff = Effect(lambda: _teardown())  # sync-looking undo producing a coroutine
    with pytest.raises(TypeError, match="awaitable"):
        eff.dispose()
    # teardown did NOT run and the handle is still live for the async path
    assert not ran and not eff.disposed


async def test_dispose_async_still_runs_awaitable_returning_undo():
    ran: list[bool] = []

    async def _teardown() -> None:
        ran.append(True)

    eff = Effect(lambda: _teardown())
    with pytest.raises(TypeError):
        eff.dispose()
    await eff.dispose_async()
    assert ran == [True] and eff.disposed
    await eff.dispose_async()  # idempotent; teardown ran exactly once
    assert ran == [True]



def test_registration_failure_is_flagged_not_silent(monkeypatch):
    """loaded=True + kernel_registered=False must be VISIBLE on the MODULES row —
    the legacy surface keeps serving the plugin while kernel consumers won't, and
    that divergence has to surface somewhere an operator looks (V6, M2 review)."""
    import pathlib as _pathlib

    import conductor.plugins as plugins_pkg
    from conductor.plugins import runtime

    def boom(module, plugin):  # noqa: ARG001
        raise RuntimeError("registration exploded")

    monkeypatch.setattr(runtime, "register_plugin", boom)
    before = len(plugins_pkg.MODULES)
    plugins: dict = {}
    routers: list = []
    search = str(_pathlib.Path(plugins_pkg.__file__).parent)
    try:
        plugins_pkg._scan(search, "conductor.plugins", plugins, routers, "shipped", set())
        rows = [r for r in plugins_pkg.MODULES[before:] if r["loaded"]]
        assert rows, "scan found nothing — fixture broken"
        assert all(r.get("kernel_registered") is False for r in rows)
        assert plugins, "legacy PLUGINS surface should still be populated"
    finally:
        del plugins_pkg.MODULES[before:]
