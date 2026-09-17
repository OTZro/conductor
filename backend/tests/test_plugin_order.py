"""User-configurable, PER-RENDER-SLOT plugin display order (set from the plugin
manager panel's "版面排序" section).

Covers: the switchboard's "order" key (slot -> module -> int) is shape-checked two
levels deep exactly like "disabled" and never crashes on a hand-edited/malformed file
— including quietly ignoring the EARLIER, never-released flat {module: int} shape;
GET /api/plugins/manifest applies the per-slot override at response time and exposes
an `orders` map per plugin; write_plugins_conf() merges instead of clobbering (a
toggle must not erase order, and reordering one slot must not erase another slot's
order); and the manager's /slots + /reorder routes list slot membership from the live
manifest and swap neighbours within one slot at a time."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from conductor.api import plugins as plugins_api
from conductor.plugins import PLUGINS, Plugin, disabled_modules, order_overrides, write_plugins_conf
from conductor.plugins.manager import router as mgr


@pytest.fixture(autouse=True)
def _isolated_conf(tmp_path, monkeypatch):
    """Every test gets its own throwaway ~/.conductor/plugins.json — never the real
    developer file."""
    conf = tmp_path / "plugins.json"
    monkeypatch.setattr("conductor.plugins._PLUGINS_CONF", conf)
    return conf


@pytest.fixture(autouse=True)
def _empty_local_dirs(tmp_path, monkeypatch):
    """Isolate /slots + /reorder from whatever local plugin dirs actually exist on the
    machine running these tests — slot membership must come only from the synthetic
    PLUGINS/MODULES each test installs."""
    monkeypatch.setattr(mgr, "_BE_LOCAL", tmp_path / "be-empty")
    monkeypatch.setattr(mgr, "_FE_LOCAL", tmp_path / "fe-empty")


def test_order_overrides_missing_file_is_empty(_isolated_conf):
    assert order_overrides() == {}


def test_order_overrides_reads_valid_per_slot_shape(_isolated_conf):
    _isolated_conf.write_text(json.dumps({"order": {"tab": {"a": 10, "b": 20}}}))
    assert order_overrides() == {"tab": {"a": 10, "b": 20}}


def test_order_overrides_ignores_non_dict_top_level(_isolated_conf):
    # a hand-edit that replaces the whole file with a list must not raise
    _isolated_conf.write_text(json.dumps(["order"]))
    assert order_overrides() == {}


def test_order_overrides_ignores_old_flat_module_to_int_shape(_isolated_conf):
    # the never-released pre-redesign shape: {"order": {module: int}} — values are
    # ints, not per-module dicts, so it must be dropped whole, not misread as one slot
    _isolated_conf.write_text(json.dumps({"order": {"mod1": 10, "mod2": 20}}))
    assert order_overrides() == {}


def test_order_overrides_ignores_non_dict_slot_value(_isolated_conf):
    _isolated_conf.write_text(json.dumps({"order": {"tab": ["a", "b"]}}))
    assert order_overrides() == {}


def test_order_overrides_drops_non_int_and_bool_entries_within_a_slot(_isolated_conf):
    # bool is an int subclass in Python, so it needs an explicit exclusion or
    # True/False would leak in as 1/0
    _isolated_conf.write_text(json.dumps({"order": {"tab": {"a": 10, "b": "ten", "c": True}}}))
    assert order_overrides() == {"tab": {"a": 10}}


def test_write_plugins_conf_merges_disabled_and_order(_isolated_conf):
    write_plugins_conf({"order": {"tab": {"x": 10}}})
    write_plugins_conf({"disabled": ["y"]})
    assert order_overrides() == {"tab": {"x": 10}}
    assert disabled_modules() == {"y"}


async def test_manifest_applies_per_slot_override_and_exposes_orders(_isolated_conf, monkeypatch):
    p1 = Plugin(id="p1", label="P1", icon="a", order=10)
    p2 = Plugin(id="p2", label="P2", icon="b", order=20)
    monkeypatch.setitem(PLUGINS, "p1", p1)
    monkeypatch.setitem(PLUGINS, "p2", p2)
    monkeypatch.setattr(plugins_api, "MODULES", [
        {"module": "mod1", "id": "p1", "source": "shipped"},
        {"module": "mod2", "id": "p2", "source": "shipped"},
    ])
    _isolated_conf.write_text(json.dumps({"order": {"tab": {"mod2": 1}}}))

    out = await plugins_api.manifest()
    m1 = next(m for m in out if m["id"] == "p1")
    m2 = next(m for m in out if m["id"] == "p2")
    # tab order overridden for p2, untouched (falls back to base) for p1
    assert m2["orders"]["tab"] == 1
    assert m1["orders"]["tab"] == 10
    # a slot with no override at all falls back to each plugin's own base order
    assert m1["orders"]["menu_bar"] == 10
    assert m2["orders"]["menu_bar"] == 20
    # top-level order/id sort is stable and untouched by the slot override
    assert m1["order"] == 10 and m2["order"] == 20


async def test_slots_lists_only_plugins_declaring_that_slot(_isolated_conf, monkeypatch):
    from conductor.plugins import CardWidgetSpec, MenuBarSpec

    async def _card(ctx):
        return None

    p_tab_only = Plugin(id="p1", label="P1", icon="a", order=10, tab=None)
    p_menu = Plugin(id="p2", label="P2", icon="b", order=20, menu_bar=MenuBarSpec(layout="x"))
    p_card = Plugin(id="p3", label="P3", icon="c", order=30, card_widget=CardWidgetSpec(title="t", layout="x", provider=_card))
    monkeypatch.setitem(PLUGINS, "p1", p_tab_only)
    monkeypatch.setitem(PLUGINS, "p2", p_menu)
    monkeypatch.setitem(PLUGINS, "p3", p_card)
    monkeypatch.setattr(mgr, "MODULES", [
        {"module": "mod1", "id": "p1", "source": "shipped"},
        {"module": "mod2", "id": "p2", "source": "shipped"},
        {"module": "mod3", "id": "p3", "source": "shipped"},
    ])

    out = await mgr.list_slots()
    assert [r["module"] for r in out["slots"]["menu_bar"]] == ["mod2"]
    assert [r["module"] for r in out["slots"]["card_widget"]] == ["mod3"]
    assert out["slots"]["tab"] == []  # none of the three declare a tab


async def test_reorder_bulk_sets_dense_order_for_one_slot(_isolated_conf, monkeypatch):
    from conductor.plugins import MenuBarSpec

    p1 = Plugin(id="p1", label="P1", icon="a", order=10, menu_bar=MenuBarSpec(layout="x"))
    p2 = Plugin(id="p2", label="P2", icon="b", order=20, menu_bar=MenuBarSpec(layout="x"))
    p3 = Plugin(id="p3", label="P3", icon="c", order=30, menu_bar=MenuBarSpec(layout="x"))
    monkeypatch.setitem(PLUGINS, "p1", p1)
    monkeypatch.setitem(PLUGINS, "p2", p2)
    monkeypatch.setitem(PLUGINS, "p3", p3)
    monkeypatch.setattr(mgr, "MODULES", [
        {"module": "mod1", "id": "p1", "source": "shipped"},
        {"module": "mod2", "id": "p2", "source": "shipped"},
        {"module": "mod3", "id": "p3", "source": "shipped"},
    ])

    out = await mgr.reorder({"order": {"menu_bar": ["mod3", "mod1", "mod2"]}})
    assert out["order"]["menu_bar"] == {"mod3": 10, "mod1": 20, "mod2": 30}
    # persisted under its own slot key — a fresh read sees the same map
    assert order_overrides()["menu_bar"] == out["order"]["menu_bar"]


async def test_reorder_bulk_handles_multiple_dirty_slots_in_one_call(_isolated_conf, monkeypatch):
    from conductor.plugins import MenuBarSpec, TabSpec

    p1 = Plugin(id="p1", label="P1", icon="a", order=10, menu_bar=MenuBarSpec(layout="x"), tab=TabSpec(layout="x"))
    p2 = Plugin(id="p2", label="P2", icon="b", order=20, menu_bar=MenuBarSpec(layout="x"), tab=TabSpec(layout="x"))
    monkeypatch.setitem(PLUGINS, "p1", p1)
    monkeypatch.setitem(PLUGINS, "p2", p2)
    monkeypatch.setattr(mgr, "MODULES", [
        {"module": "mod1", "id": "p1", "source": "shipped"},
        {"module": "mod2", "id": "p2", "source": "shipped"},
    ])

    await mgr.reorder({"order": {"tab": ["mod2", "mod1"], "menu_bar": ["mod1", "mod2"]}})
    assert order_overrides()["tab"] == {"mod2": 10, "mod1": 20}
    assert order_overrides()["menu_bar"] == {"mod1": 10, "mod2": 20}


async def test_reorder_bulk_preserves_untouched_slots_across_calls(_isolated_conf, monkeypatch):
    from conductor.plugins import MenuBarSpec, TabSpec

    p1 = Plugin(id="p1", label="P1", icon="a", order=10, menu_bar=MenuBarSpec(layout="x"), tab=TabSpec(layout="x"))
    p2 = Plugin(id="p2", label="P2", icon="b", order=20, menu_bar=MenuBarSpec(layout="x"), tab=TabSpec(layout="x"))
    monkeypatch.setitem(PLUGINS, "p1", p1)
    monkeypatch.setitem(PLUGINS, "p2", p2)
    monkeypatch.setattr(mgr, "MODULES", [
        {"module": "mod1", "id": "p1", "source": "shipped"},
        {"module": "mod2", "id": "p2", "source": "shipped"},
    ])

    await mgr.reorder({"order": {"tab": ["mod2", "mod1"]}})
    tab_after_first = order_overrides()["tab"]
    await mgr.reorder({"order": {"menu_bar": ["mod2", "mod1"]}})
    # reordering menu_bar in a SEPARATE call must not have clobbered the earlier tab write
    assert order_overrides()["tab"] == tab_after_first
    assert order_overrides()["menu_bar"] == {"mod2": 10, "mod1": 20}


async def test_reorder_rejects_bad_body_shape(_isolated_conf, monkeypatch):
    with pytest.raises(HTTPException):
        await mgr.reorder({})
    with pytest.raises(HTTPException):
        await mgr.reorder({"order": {}})
    with pytest.raises(HTTPException):
        await mgr.reorder({"order": {"menu_bar": "not-a-list"}})


async def test_reorder_rejects_unknown_slot(_isolated_conf, monkeypatch):
    monkeypatch.setattr(mgr, "MODULES", [])
    with pytest.raises(HTTPException):
        await mgr.reorder({"order": {"bogus-slot": ["mod1"]}})


async def test_reorder_rejects_module_not_known_for_that_slot(_isolated_conf, monkeypatch):
    from conductor.plugins import MenuBarSpec

    p1 = Plugin(id="p1", label="P1", icon="a", order=10, menu_bar=MenuBarSpec(layout="x"))
    monkeypatch.setitem(PLUGINS, "p1", p1)
    monkeypatch.setattr(mgr, "MODULES", [{"module": "mod1", "id": "p1", "source": "shipped"}])

    with pytest.raises(HTTPException):  # unknown module entirely
        await mgr.reorder({"order": {"menu_bar": ["mod1", "not-a-real-module"]}})
    with pytest.raises(HTTPException):  # mod1 doesn't declare "tab"
        await mgr.reorder({"order": {"tab": ["mod1"]}})


async def test_reorder_rejects_stale_module_set(_isolated_conf, monkeypatch):
    from conductor.plugins import MenuBarSpec

    p1 = Plugin(id="p1", label="P1", icon="a", order=10, menu_bar=MenuBarSpec(layout="x"))
    p2 = Plugin(id="p2", label="P2", icon="b", order=20, menu_bar=MenuBarSpec(layout="x"))
    monkeypatch.setitem(PLUGINS, "p1", p1)
    monkeypatch.setitem(PLUGINS, "p2", p2)
    monkeypatch.setattr(mgr, "MODULES", [
        {"module": "mod1", "id": "p1", "source": "shipped"},
        {"module": "mod2", "id": "p2", "source": "shipped"},
    ])
    # missing mod2 — a stale panel copy must not silently commit a partial order
    with pytest.raises(HTTPException):
        await mgr.reorder({"order": {"menu_bar": ["mod1"]}})
    # a duplicate is equally a corrupt payload
    with pytest.raises(HTTPException):
        await mgr.reorder({"order": {"menu_bar": ["mod1", "mod1"]}})
