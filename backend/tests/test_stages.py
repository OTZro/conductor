"""The stage system: registry (built-ins + lanes.json + plugin StageSpecs, validated,
last-known-good), lane arbitration (ball derivation always wins; manual > claims >
built-in chain inside the human space), the /api/board/lanes endpoint, the PATCH
/state manual move, auto-clear on done, registry-driven list rank, and lane-change
events to plugins. sqlite-backed like test_files (no postgres)."""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor import lanes as lanes_mod
from conductor import store
from conductor.config import settings
from conductor.lanes import lane_for_card, stage_entry, stage_registry
from conductor.main import app
from conductor.plugins import PLUGINS, LaneChangeSpec, Plugin, StageSpec
from conductor.plugins.src_jira import jira as jira_src
from conductor.plugins.stages import _card_data as stage_widget


@pytest.fixture
def lanes_file(tmp_path, monkeypatch):
    """A lanes.json with one custom stage, wired into settings with a fresh cache."""
    path = tmp_path / "lanes.json"
    path.write_text(json.dumps({"stages": [{"key": "pending", "title": "Pending", "order": 5}]}))
    monkeypatch.setattr(settings, "lanes_file", path)
    monkeypatch.setattr(lanes_mod, "_config_stages", lanes_mod._ConfigStages())
    return path


@pytest.fixture
async def client(tmp_path, monkeypatch, lanes_file):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/stages-test.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c
    await engine.dispose()


async def _make_card(client) -> str:
    return (await client.post("/api/cards", json={"title": "stage test"})).json()["id"]


# ── registry ──────────────────────────────────────────────────────────────────────


def test_registry_merges_and_orders(lanes_file):
    keys = [lane["key"] for lane in stage_registry()]
    assert keys == ["need_human", "pending", "ai_working", "backlog", "done"]


def test_registry_validation_skips_bad_entries(lanes_file, monkeypatch):
    lanes_file.write_text(json.dumps({"stages": [
        {"key": "ok-stage"},
        {"key": "BAD KEY"},              # not a slug
        {"key": "need_human"},           # builtin collision
        {"key": "ai-stage", "within": "ai"},  # v1 guard: human only
        {"key": "toned", "tone": "neon"},     # unknown tone → clamped, kept
    ]}))
    monkeypatch.setattr(lanes_mod, "_config_stages", lanes_mod._ConfigStages())
    by_key = {lane["key"]: lane for lane in stage_registry()}
    assert "ok-stage" in by_key and "toned" in by_key
    assert by_key["toned"]["tone"] == "backlog"  # clamped, not dropped
    assert "BAD KEY" not in by_key and "ai-stage" not in by_key
    assert by_key["need_human"]["builtin"] is True  # builtin won the collision


def test_registry_keeps_last_known_good_on_corrupt_file(lanes_file):
    assert any(lane["key"] == "pending" for lane in stage_registry())
    lanes_file.write_text("{not json")
    assert any(lane["key"] == "pending" for lane in stage_registry())  # LKG, not builtins-only


def test_plugin_stage_spec_joins_registry(lanes_file, monkeypatch):
    fake = Plugin(
        id="zz-stager", label="Z", icon="🧪",
        stages=(StageSpec(key="review-queue", title="Review Queue", order=6),),
    )
    monkeypatch.setitem(PLUGINS, "zz-stager", fake)
    keys = [lane["key"] for lane in stage_registry()]
    assert keys.index("pending") < keys.index("review-queue") < keys.index("ai_working")


# ── arbitration (pure function) ───────────────────────────────────────────────────

_HUMAN = {"jira": {"status": "In Progress", "status_category": "indeterminate"}}


def test_ball_derivation_always_wins(lanes_file):
    assert lane_for_card(_HUMAN, "none", manual_stage="pending") == "done"
    assert lane_for_card(_HUMAN, "ai", manual_stage="pending") == "ai_working"


def test_manual_beats_claims_beats_builtin_chain(lanes_file):
    claimed = dict(_HUMAN, stages={"botA": "backlog"})
    assert lane_for_card(claimed, "human", manual_stage="pending") == "pending"  # manual first
    assert lane_for_card(claimed, "human") == "backlog"  # then claims
    assert lane_for_card(_HUMAN, "human") == "need_human"  # then derivation


def test_claim_arbitration_is_registry_display_order(lanes_file):
    two = dict(_HUMAN, stages={"a": "backlog", "b": "pending"})
    assert lane_for_card(two, "human") == "pending"  # order 5 < 20, claimant names irrelevant


def test_unknown_or_cleared_stage_falls_through(lanes_file):
    assert lane_for_card(_HUMAN, "human", manual_stage="ghost") == "need_human"
    cleared = dict(_HUMAN, stages={"bot": None})
    assert lane_for_card(cleared, "human") == "need_human"


def test_manual_park_beats_live_waiting(lanes_file):
    """Parking means "I know it needs me, I'm deferring it" — a manually-parked card
    stays parked even while its claude waits (the waiting still shows ON the card;
    only the column changes). This is the PROJ-10367 case."""
    waiting = dict(_HUMAN, agent={"waiting": True})
    assert lane_for_card(waiting, "human", manual_stage="pending") == "pending"


def test_live_waiting_still_beats_robot_claims(lanes_file):
    """Without a manual park, a live human-blocking prompt must not be hidden by a
    plugin's automated placement."""
    waiting = dict(_HUMAN, agent={"waiting": True}, stages={"bot": "pending"})
    assert lane_for_card(waiting, "human") == "need_human"


@pytest.fixture
def count_registry_builds(monkeypatch):
    """Counts EVERY stage_registry() build, wherever it was called from.

    Patching `lanes.stage_registry` does not do that: api/cards and api/actions bind the
    name at import (`from ..lanes import stage_registry`), so their own builds are
    invisible to it — the count came back a deterministic 0, and moving the build back
    inside the per-card loop left the suite green. Patch the callee instead:
    `stage_registry` calls `_plugin_stages` module-qualified, so a patch here is seen no
    matter which module entered."""
    builds = 0
    real = lanes_mod._plugin_stages

    def counting(taken):
        nonlocal builds
        builds += 1
        return real(taken)

    monkeypatch.setattr(lanes_mod, "_plugin_stages", counting)
    return lambda: builds


def test_a_caller_supplied_registry_is_the_lane_set_that_decides(lanes_file):
    """Not a differential test. Comparing `lane_for_card(..., registry=r)` against
    `lane_for_card(...)` is a tautology w.r.t. the contract that matters: both agree
    whenever the passed registry happens to equal the built one, which is always the
    case in a test, so a half-applied hoist that silently ignored `registry` would pass.

    Inject a stage that exists in NO lanes.json and NO plugin, and the answer can only
    come from the argument."""
    # built through the real normalizer, not hand-written: a literal copy of the entry
    # shape drifts the moment a field is added, and an entry missing a field is exactly
    # what used to KeyError in the claims loop
    injected = [lanes_mod._normalize_stage(
        {"key": "escalated", "title": "Escalated", "order": 1}, "test", set()
    )]
    # a manual override the real registry has never heard of
    assert lane_for_card(_HUMAN, "human", manual_stage="escalated", registry=injected) == "escalated"
    assert lane_for_card(_HUMAN, "human", manual_stage="escalated") == "need_human"
    # ...and a claim, which resolves through the display-order loop rather than the lookup
    claimed = dict(_HUMAN, stages={"bot": "escalated"})
    assert lane_for_card(claimed, "human", registry=injected) == "escalated"
    assert lane_for_card(claimed, "human") == "need_human"
    # a stage the injected set does NOT carry must still fall through, not error
    assert lane_for_card(_HUMAN, "human", manual_stage="pending", registry=injected) == "need_human"


def test_a_partial_entry_falls_through_instead_of_raising(lanes_file):
    """`registry` is public, so a caller can hand in an entry the normalizer never saw.
    Both arbitration branches must treat a missing field as "unknown stage" and fall
    through. The manual branch read it with .get and the claims loop with a subscript,
    so the same partial entry returned need_human on one path and raised KeyError on the
    other — inside the board's placement authority."""
    partial = [{"key": "half"}]  # no `within`
    assert lane_for_card(_HUMAN, "human", manual_stage="half", registry=partial) == "need_human"
    claimed = dict(_HUMAN, stages={"bot": "half"})
    assert lane_for_card(claimed, "human", registry=partial) == "need_human"
    # an entry with no key at all must not match either
    assert lane_for_card(_HUMAN, "human", manual_stage="half", registry=[{}]) == "need_human"


def test_stage_entry_finds_by_key_and_reads_the_passed_registry(lanes_file):
    """The helper both call sites route through, tested directly rather than only via
    its callers."""
    assert stage_entry("pending")["title"] == "Pending"       # falls back to a built one
    assert stage_entry("nope") is None
    injected = [{"key": "only-here", "within": "human"}]
    assert stage_entry("only-here", registry=injected)["within"] == "human"
    assert stage_entry("pending", registry=injected) is None  # the argument is the whole world
    assert stage_entry("x", registry=[{}]) is None            # keyless entries don't match


def test_ball_short_circuits_before_the_registry_is_read(lanes_file, count_registry_builds):
    """ai/none return before any registry lookup, so a caller hoisting a snapshot for
    such a card is doing pure waste — store.upsert_card was measurably slower than
    before for exactly this reason. Asserted as a side effect (no build happened), not
    as an equality: a differential form cannot observe whether a registry was built."""
    before = count_registry_builds()  # delta, like its siblings — absolute counts here
    assert lane_for_card(_HUMAN, "ai", manual_stage="pending") == "ai_working"
    assert lane_for_card(_HUMAN, "none", manual_stage="pending") == "done"
    assert count_registry_builds() - before == 0

    before = count_registry_builds()
    assert lane_for_card(_HUMAN, "human") == "need_human"
    assert count_registry_builds() - before == 1  # ...and the human path does read it


async def test_the_board_builds_the_stage_registry_once_per_request(
    client, count_registry_builds
):
    """It used to be rebuilt inside lane_for_card for EVERY card — a lanes.json stat
    plus a full plugin-StageSpec re-normalize, merge and sort each time, on an endpoint
    hit every 15s and on every WS event. Exactly one build serves the whole response."""
    for _ in range(5):
        await _make_card(client)
    before = count_registry_builds()

    r = await client.get("/api/cards")

    assert r.status_code == 200
    assert len(r.json()) == 5
    built = count_registry_builds() - before
    assert built == 1, f"the registry was built {built}x for a 5-card board — expected 1"


async def test_a_stage_move_answers_the_client_from_the_lane_set_it_judged(
    client, count_registry_builds
):
    """PATCH /state derives old_lane, commits, derives new_lane, then serializes the
    response. All three must see ONE lane set: the snapshot used to stop short of the
    response body, so the `lane` the client rendered and the old->new pair plugins
    received could come from different registries if lanes.json was saved during the
    commit. One build for the whole request is what pins that."""
    cid = await _make_card(client)
    before = count_registry_builds()

    r = await client.patch(f"/api/cards/{cid}/state", json={"stage": "pending"})

    assert r.status_code == 200
    assert r.json()["lane"] == "pending"
    built = count_registry_builds() - before
    assert built == 1, f"PATCH /state built the registry {built}x — expected 1 for the request"


# ── API surface ───────────────────────────────────────────────────────────────────


async def test_board_lanes_endpoint(client):
    lanes = (await client.get("/api/board/lanes")).json()
    assert [lane["key"] for lane in lanes] == ["need_human", "pending", "ai_working", "backlog", "done"]
    pending = next(lane for lane in lanes if lane["key"] == "pending")
    assert pending["builtin"] is False and pending["within"] == "human"


async def test_manual_move_roundtrip_and_clear(client):
    cid = await _make_card(client)
    assert (await client.get(f"/api/cards/{cid}")).json()["lane"] == "need_human"  # manual note
    moved = await client.patch(f"/api/cards/{cid}/state", json={"stage": "pending"})
    assert moved.json()["lane"] == "pending" and moved.json()["local"]["manual_stage"] == "pending"
    cleared = await client.patch(f"/api/cards/{cid}/state", json={"stage": None})
    assert cleared.json()["lane"] == "need_human" and cleared.json()["local"]["manual_stage"] is None


async def test_manual_move_rejects_unknown_stage(client):
    cid = await _make_card(client)
    assert (await client.patch(f"/api/cards/{cid}/state", json={"stage": "ghost"})).status_code == 400
    assert (await client.patch(f"/api/cards/{cid}/state", json={"stage": "ai_working"})).status_code == 400


async def test_manual_stage_autoclears_when_card_reaches_done(client):
    cid = await _make_card(client)
    await client.patch(f"/api/cards/{cid}/state", json={"stage": "pending"})
    await client.post(f"/api/cards/{cid}/done", json={"done": True})  # manual card → ball none
    got = (await client.get(f"/api/cards/{cid}")).json()
    assert got["lane"] == "done" and got["local"]["manual_stage"] is None
    # reopen derives fresh — no resurrection into pending
    await client.post(f"/api/cards/{cid}/done", json={"done": False})
    assert (await client.get(f"/api/cards/{cid}")).json()["lane"] == "need_human"


async def test_list_sorts_custom_stage_by_registry_rank(client):
    """A pending card ranks BELOW need_human (0 < 5) but ABOVE done — the flat list
    keeps urgent cards on top and parked ones under them."""
    pend = await _make_card(client)
    urgent = await _make_card(client)
    finished = await _make_card(client)
    await client.patch(f"/api/cards/{pend}/state", json={"stage": "pending"})
    await client.post(f"/api/cards/{finished}/done", json={"done": True})
    ids = (pend, urgent, finished)
    listed = [c["id"] for c in (await client.get("/api/cards")).json() if c["id"] in ids]
    assert listed == [urgent, pend, finished]  # rank: need_human 0 < pending 5 < done 30


async def test_lane_change_event_fires_on_manual_move(client, monkeypatch):
    seen: list[dict] = []

    async def cb(ctx: dict) -> None:
        seen.append(ctx)

    fake = Plugin(id="zz-observer", label="Z", icon="🧪", lane_changes=(LaneChangeSpec(callback=cb),))
    monkeypatch.setitem(PLUGINS, "zz-observer", fake)
    cid = await _make_card(client)
    await client.patch(f"/api/cards/{cid}/state", json={"stage": "pending"})
    await asyncio.sleep(0.05)  # fire-and-forget tasks drain
    assert seen and seen[-1]["old_lane"] == "need_human" and seen[-1]["new_lane"] == "pending"
    assert seen[-1]["card_id"] == cid and seen[-1]["ball"] == "human"


async def test_lane_change_event_fires_on_upsert_transition(client, monkeypatch):
    seen: list[dict] = []

    async def cb(ctx: dict) -> None:
        seen.append(ctx)

    fake = Plugin(id="zz-observer2", label="Z", icon="🧪", lane_changes=(LaneChangeSpec(callback=cb),))
    monkeypatch.setitem(PLUGINS, "zz-observer2", fake)
    cid = await _make_card(client)
    await client.post(f"/api/cards/{cid}/done", json={"done": True})  # need_human → done
    await asyncio.sleep(0.05)
    assert any(c["old_lane"] == "need_human" and c["new_lane"] == "done" for c in seen)


async def test_lane_change_callback_error_is_swallowed(client, monkeypatch):
    async def boom(ctx: dict) -> None:
        raise RuntimeError("plugin bug")

    fake = Plugin(id="zz-broken", label="Z", icon="🧪", lane_changes=(LaneChangeSpec(callback=boom),))
    monkeypatch.setitem(PLUGINS, "zz-broken", fake)
    cid = await _make_card(client)
    r = await client.patch(f"/api/cards/{cid}/state", json={"stage": "pending"})
    assert r.status_code == 200  # the move itself is unaffected
    await asyncio.sleep(0.05)
    assert (await client.get(f"/api/cards/{cid}")).json()["lane"] == "pending"


async def test_stage_widget_offers_choices_and_tracks_current(client):
    """The stages PLUGIN provides the move UI: choices = all human-space stages once a
    custom one exists, `current` mirrors the manual override."""
    cid = await _make_card(client)
    got = (await client.get(f"/api/cards/{cid}")).json()
    ctx = {"origin": got["origin"], "external_id": got["external_id"], "links": []}
    out = await stage_widget(ctx)
    assert out and out["card_id"] == cid and out["current"] is None
    assert [c["key"] for c in out["choices"]] == ["need_human", "pending", "backlog"]
    await client.patch(f"/api/cards/{cid}/state", json={"stage": "pending"})
    assert (await stage_widget(ctx))["current"] == "pending"


async def test_stage_widget_manifests_into_the_actions_slot(client):
    """The stages plugin declares slot="actions" so the FE renders it inline in the
    card detail's action row; other card widgets default to the body slot."""
    manifest = (await client.get("/api/plugins/manifest")).json()
    stages = next(p for p in manifest if p["id"] == "stages")
    assert stages["card_widget"]["slot"] == "actions"
    files = next(p for p in manifest if p["id"] == "files")
    assert files["card_widget"]["slot"] == "body"


async def test_stage_widget_hidden_on_stock_board(client, tmp_path, monkeypatch):
    """No custom stage registered (stock 4-lane board) → the widget provider returns
    None, so a stock install shows no move control anywhere."""
    monkeypatch.setattr(settings, "lanes_file", tmp_path / "absent.json")
    monkeypatch.setattr(lanes_mod, "_config_stages", lanes_mod._ConfigStages())
    cid = await _make_card(client)
    got = (await client.get(f"/api/cards/{cid}")).json()
    ctx = {"origin": got["origin"], "external_id": got["external_id"], "links": []}
    assert await stage_widget(ctx) is None


async def test_plugin_claim_places_card_via_upsert(client):
    """The programmatic path: a plugin writes a stage claim through upsert_card and the
    card lands in that stage; clearing the claim returns it to the derived lane."""
    cid = await _make_card(client)
    got = (await client.get(f"/api/cards/{cid}")).json()
    async with db_mod.session_maker() as s:
        await store.upsert_card(
            s, origin=got["origin"], external_id=got["external_id"],
            cached_patch={"stages": {"zz-claimer": "pending"}},
        )
    assert (await client.get(f"/api/cards/{cid}")).json()["lane"] == "pending"
    async with db_mod.session_maker() as s:
        await store.upsert_card(
            s, origin=got["origin"], external_id=got["external_id"],
            cached_patch={"stages": {"zz-claimer": None}},
        )
    assert (await client.get(f"/api/cards/{cid}")).json()["lane"] == "need_human"


# ── the store's own registry discipline ───────────────────────────────────────────
# Four separate mutations of store.upsert_card's registry line used to leave the whole
# suite green: making the build unconditional (the ai/none perf regression), dropping
# the snapshot entirely, narrowing the guard to `ball == "human"` (which loses the
# human->ai transition), and removing `registry=` from both lane_for_card calls. The
# fix had no test; these are it.


async def _upsert(client, ball_patch: dict, *, external_id: str, **kw) -> None:
    async with db_mod.session_maker() as s:
        await store.upsert_card(
            s, origin="jira", external_id=external_id, cached_patch=ball_patch, **kw
        )


# cached_patch MERGES, so each of these must actively clear the others' signal — a
# patch that only sets its own would leave `agent.running` true from a previous
# upsert and pin the ball to ai regardless of what the jira half says.
_BALL_PATCH = {
    "human": {"jira": {"status": "Building", "assignee_me": True},
              "agent": {"running": False}},
    "ai": {"jira": {"status": "Building", "assignee_me": True},
           "agent": {"running": True}},
    "none": {"jira": {"status": "Done", "assignee_me": True},
             "agent": {"running": False}},
}


@pytest.mark.parametrize(
    "first,second,expected_builds",
    [
        ("human", "human", 1),  # both derivations read it — one shared snapshot
        ("human", "ai", 1),     # prev_ball human still needs the old-lane derivation
        ("ai", "human", 1),     # ...and the new-lane one does
        ("ai", "ai", 0),        # neither side reaches the registry
        ("none", "ai", 0),
        ("ai", "none", 0),
    ],
)
async def test_upsert_builds_the_registry_only_when_a_lane_needs_it(
    client, count_registry_builds, first, second, expected_builds
):
    """lane_for_card returns for ball ai/none BEFORE reading the registry, so building
    one for such a card is pure waste — an unconditional build made ai/none upserts do
    strictly more work than before the parameter existed."""
    eid = f"BALL-{first}-{second}"
    await _upsert(client, _BALL_PATCH[first], external_id=eid)   # create
    before = count_registry_builds()
    await _upsert(client, _BALL_PATCH[second], external_id=eid)  # the not-created path
    built = count_registry_builds() - before
    assert built == expected_builds, (
        f"{first}->{second} built the registry {built}x, expected {expected_builds}"
    )


async def test_upsert_uses_the_caller_snapshot_instead_of_building_its_own(
    client, count_registry_builds
):
    """An endpoint that upserts and then serializes must be able to hand ONE snapshot to
    both, or the lane_change event plugins receive and the lane the response renders come
    from two different reads of lanes.json."""
    eid = "SNAP-1"
    await _upsert(client, _BALL_PATCH["human"], external_id=eid)
    registry = stage_registry()
    before = count_registry_builds()
    await _upsert(client, _BALL_PATCH["human"], external_id=eid, registry=registry)
    assert count_registry_builds() - before == 0, "the caller's snapshot was ignored"


async def test_a_done_toggle_answers_from_the_lane_set_it_judged(
    client, count_registry_builds
):
    """POST /done upserts (which derives old/new lane) then serializes the response.
    Both must see one lane set — this handler had the same divergence PATCH /state was
    fixed for, one endpoint over."""
    cid = await _make_card(client)
    before = count_registry_builds()
    r = await client.post(f"/api/cards/{cid}/done", json={"done": False})
    assert r.status_code == 200
    built = count_registry_builds() - before
    assert built == 1, f"POST /done built the registry {built}x — expected 1 per request"


async def test_marking_an_already_done_card_done_builds_nothing(
    client, count_registry_builds
):
    """The mirror of the store's own ai/none guard, one layer up. A done->done toggle
    derives no lane anywhere — the store skips its build (ball none both sides) and
    lane_for_card returns "done" before reading a registry — so the handler must not
    build one either. An unconditional build here is one where master did zero, on a
    board full of Done cards.

    The done=False direction is asserted alongside it so the gate can't be "fixed" by
    never building at all."""
    cid = await _make_card(client)
    await client.post(f"/api/cards/{cid}/done", json={"done": True})  # → ball none

    before = count_registry_builds()
    r = await client.post(f"/api/cards/{cid}/done", json={"done": True})  # already done
    assert r.status_code == 200 and r.json()["lane"] == "done"
    built = count_registry_builds() - before
    assert built == 0, f"a done->done toggle built the registry {built}x — expected 0"

    before = count_registry_builds()
    r = await client.post(f"/api/cards/{cid}/done", json={"done": False})  # → ball human
    assert r.status_code == 200 and r.json()["lane"] == "need_human"
    built = count_registry_builds() - before
    assert built == 1, f"a reopen built the registry {built}x — expected 1 shared snapshot"


async def test_a_create_costs_exactly_one_registry_build(client, count_registry_builds):
    """POST /api/cards was the one snapshot site with no test: dropping the hoist left
    the whole suite green, where the same mutation on PATCH /state or POST /done turns
    it red. `external_id=new_id()` makes the upsert always a create, so the store
    derives no lane and the response's own serialize is the single reader — one build
    for the request, and this pins it against a refactor that reintroduces a second."""
    before = count_registry_builds()

    r = await client.post("/api/cards", json={"title": "one build per create"})

    assert r.status_code == 200 and r.json()["lane"] == "need_human"
    built = count_registry_builds() - before
    assert built == 1, f"POST /api/cards built the registry {built}x — expected 1"


async def test_a_hold_toggle_answers_from_the_lane_set_it_judged(
    client, count_registry_builds, monkeypatch
):
    """POST /hold is the genuine TWO-read case: jira_src.set_hold upserts in its own
    session (so it derives the lane_change), then the handler serializes the response.
    Flipping the hold label is exactly what moves the ball to or from `human`, so both
    halves derive a lane — one snapshot has to cross the session boundary."""
    monkeypatch.setattr(jira_src, "session_maker", db_mod.session_maker)

    async def fake_acli(args, timeout=30):
        return ""

    monkeypatch.setattr(jira_src, "_run_acli", fake_acli)

    eid = "HOLD-1"
    await _upsert(client, _BALL_PATCH["human"], external_id=eid)
    cid = next(
        c["id"] for c in (await client.get("/api/cards")).json() if c["external_id"] == eid
    )

    before = count_registry_builds()
    r = await client.post(f"/api/cards/{cid}/hold", json={"hold": True})

    assert r.status_code == 200
    built = count_registry_builds() - before
    assert built == 1, f"POST /hold built the registry {built}x — expected 1 per request"


async def test_both_lane_derivations_share_one_snapshot(client, monkeypatch):
    """Build counts cannot catch this. Narrow the guard to `ball == "human"` and a
    human->ai transition STILL totals one build — old_lane simply builds its own
    internally instead of receiving the shared one. The defect is not the count, it is
    that the two lanes being compared were judged against two different reads of
    lanes.json, which is what manufactures a phantom transition. So assert identity:
    both derivations must receive the SAME object."""
    seen: list[object] = []
    real = store.lane_for_card

    def recording(cached, ball, hold_label="conductor-hold", manual_stage=None, *, registry=None):
        seen.append(registry)
        return real(cached, ball, hold_label, manual_stage, registry=registry)

    eid = "SHARED-1"
    await _upsert(client, _BALL_PATCH["human"], external_id=eid)
    monkeypatch.setattr(store, "lane_for_card", recording)
    await _upsert(client, _BALL_PATCH["ai"], external_id=eid)  # human -> ai

    assert len(seen) == 2, f"expected old+new derivations, saw {len(seen)}"
    assert seen[0] is not None, "the old-lane derivation fell back to its own build"
    assert seen[0] is seen[1], "the two derivations judged against different registries"
