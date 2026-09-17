"""conductor.kernel unit tests: the Service / Typed-Event / Effect trio in
isolation — no app, no db. Pins the semantics M2+ integration will lean on:
exactly-one-active provider with override/priority, deterministic dispatch
ordering, waterfall short-circuit (the lanes shape), notify error isolation,
and Effect-based teardown (the unload primitive)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

import pytest

from conductor.kernel import (
    CompositeEffect,
    DuplicateProviderError,
    Effect,
    Event,
    Kernel,
    ServiceDefinition,
    Transform,
    UnknownServiceError,
    kernel as default_kernel,
    reset_kernel,
)


@pytest.fixture(autouse=True)
def _fresh_default_kernel():
    """The default kernel is a module singleton; wipe it around every test so
    one test's registrations can't leak into another's."""
    reset_kernel()
    yield
    reset_kernel()


@dataclass(frozen=True)
class Ping(Event):
    value: int = 0


@dataclass(frozen=True)
class Other(Event):
    value: int = 0


GREETER: ServiceDefinition[object] = ServiceDefinition(
    name="test.greeter", version="1", contract="callable() -> str"
)


# ── services: exactly-one-active provider ────────────────────────────────────


def test_register_and_get_by_definition_and_name():
    k = Kernel()
    k.services.register_provider(GREETER, "hello")
    assert k.services.get(GREETER) == "hello"
    assert k.services.get("test.greeter") == "hello"  # string key resolves the same seam


def test_unknown_service_error_is_loud_and_names_known_services():
    k = Kernel()
    k.services.register_provider(GREETER, "hello")
    with pytest.raises(UnknownServiceError) as exc:
        k.services.get("test.nope")
    assert "test.nope" in str(exc.value) and "test.greeter" in str(exc.value)


def test_same_priority_without_override_raises_at_registration():
    """A silent priority tie ANYWHERE in the stack is a config bug — it must
    fail at load time, not resolve nondeterministically at first use."""
    k = Kernel()
    k.services.register_provider(GREETER, "a")
    with pytest.raises(DuplicateProviderError):
        k.services.register_provider(GREETER, "b")
    assert k.services.get(GREETER) == "a"  # failed registration left no residue


def test_non_top_priority_tie_also_raises():
    """The buried-tie hole: base(0) → vip(10) → base2(0) used to pass, and
    once vip disposed, base2 won by registration order — exactly the
    last-import-wins the seam exists to kill. The tie scan covers the whole
    stack, not just the top."""
    k = Kernel()
    k.services.register_provider(GREETER, "base")
    vip = k.services.register_provider(GREETER, "vip", priority=10)
    with pytest.raises(DuplicateProviderError):
        k.services.register_provider(GREETER, "base2")  # tied with shadowed base
    k.services.register_provider(GREETER, "base2", override=True)  # explicit intent OK
    vip.dispose()
    assert k.services.get(GREETER) == "base2"  # active by declared override, not accident


def test_override_shadows_and_dispose_restores():
    """The plugin-unload story: an override wins while registered, and
    disposing its Effect reactivates the provider underneath."""
    k = Kernel()
    k.services.register_provider(GREETER, "base")
    eff = k.services.register_provider(GREETER, "shadow", override=True)
    assert k.services.get(GREETER) == "shadow"
    eff.dispose()
    assert k.services.get(GREETER) == "base"


def test_higher_priority_wins_without_override_flag():
    k = Kernel()
    k.services.register_provider(GREETER, "base")
    k.services.register_provider(GREETER, "vip", priority=10)
    assert k.services.get(GREETER) == "vip"
    # lower-priority late arrival slots underneath silently
    k.services.register_provider(GREETER, "understudy", priority=-5)
    assert k.services.get(GREETER) == "vip"


def test_disposing_last_provider_makes_service_unknown():
    k = Kernel()
    eff = k.services.register_provider(GREETER, "only")
    eff.dispose()
    with pytest.raises(UnknownServiceError):
        k.services.get(GREETER)


def test_providers_introspection_lists_stack_with_active_flag():
    """The /api/status-style advertising surface: full stack, active first,
    active computed by the registry (callers never re-derive selection)."""
    k = Kernel()
    k.services.register_provider(GREETER, "base")
    k.services.register_provider(GREETER, "vip", priority=10)
    rows = k.services.providers(GREETER)
    assert [(r.provider, r.active) for r in rows] == [("vip", True), ("base", False)]
    assert all(r.service == "test.greeter" for r in rows)
    assert k.services.providers("test.nope") == ()  # introspection never raises


def test_inject_resolves_at_call_time_and_explicit_kwarg_wins():
    """Late binding is the point: the consumer is decorated before any
    provider exists, and still resolves once one shows up. An explicitly
    passed kwarg beats injection so tests can hand in fakes."""
    k = Kernel()

    @k.services.inject(greeter=GREETER)
    def use(greeter=None):
        return greeter

    with pytest.raises(UnknownServiceError):
        use()  # nothing registered yet → loud, not None
    k.services.register_provider(GREETER, "late")
    assert use() == "late"
    assert use(greeter="fake") == "fake"


async def test_inject_works_on_async_functions():
    k = Kernel()
    k.services.register_provider(GREETER, "async-hello")

    @k.services.inject(greeter=GREETER)
    async def use(greeter=None):
        return greeter

    assert await use() == "async-hello"


# ── events: ordering, modes, short-circuit, isolation ────────────────────────


async def test_notify_orders_by_priority_then_registration_and_awaits_async():
    k = Kernel()
    seen: list[str] = []

    async def a(e):
        seen.append("a")

    k.events.subscribe(Ping, lambda e: seen.append("z-late"), priority=10)
    k.events.subscribe(Ping, a, priority=0)
    k.events.subscribe(Ping, lambda e: seen.append("b"), priority=0)  # same prio → reg order
    await k.events.notify(Ping())
    assert seen == ["a", "b", "z-late"]


async def test_notify_isolates_handler_errors():
    """One broken observer must never break the emitter or its siblings."""
    k = Kernel()
    seen: list[str] = []

    def boom(e):
        raise RuntimeError("broken plugin")

    k.events.subscribe(Ping, boom, priority=0)
    k.events.subscribe(Ping, lambda e: seen.append("ok"), priority=1)
    await k.events.notify(Ping())
    assert seen == ["ok"]


def test_notify_sync_skips_async_handler_without_raising():
    k = Kernel()
    seen: list[str] = []

    async def slow(e):
        seen.append("never")

    k.events.subscribe(Ping, slow)
    k.events.subscribe(Ping, lambda e: seen.append("ok"), priority=1)
    k.events.notify_sync(Ping())
    assert seen == ["ok"]


async def test_waterfall_first_verdict_short_circuits():
    """The lanes shape: rules at declared priorities, first non-None wins,
    later rules never even run."""
    k = Kernel()
    calls: list[str] = []

    def rule1(e):
        calls.append("rule1")
        return None  # abstain

    def rule2(e):
        calls.append("rule2")
        return ("human", "needs you")

    def rule3(e):
        calls.append("rule3")
        return ("none", "unreachable")

    k.events.subscribe(Ping, rule1, priority=1)
    k.events.subscribe(Ping, rule2, priority=2)
    k.events.subscribe(Ping, rule3, priority=3)
    assert await k.events.waterfall(Ping()) == ("human", "needs you")
    assert calls == ["rule1", "rule2"]


def test_waterfall_sync_matches_async_semantics():
    k = Kernel()
    k.events.subscribe(Ping, lambda e: None, priority=1)
    k.events.subscribe(Ping, lambda e: "verdict", priority=2)
    assert k.events.waterfall_sync(Ping()) == "verdict"


async def test_waterfall_all_abstain_returns_the_event_itself():
    """No verdict → the (untransformed) event comes back, so callers can
    distinguish "no rule fired" from a None-valued verdict (impossible —
    None always means abstain) and transform-only chains compose."""
    k = Kernel()
    k.events.subscribe(Ping, lambda e: None)
    ping = Ping(value=7)
    assert await k.events.waterfall(ping) is ping
    assert k.events.waterfall_sync(ping) is ping


async def test_waterfall_transform_wrapper_replaces_payload_downstream():
    """Only an explicit Transform(...) is a transform: the chain continues and
    later handlers see the replacement."""
    k = Kernel()

    k.events.subscribe(Ping, lambda e: Transform(replace(e, value=e.value + 1)), priority=1)
    k.events.subscribe(Ping, lambda e: ("saw", e.value), priority=2)
    assert await k.events.waterfall(Ping(value=41)) == ("saw", 42)


def test_waterfall_bare_event_return_is_a_verdict_not_a_transform():
    """The hijack fix: an Event-typed VERDICT (even same-type) short-circuits
    like any non-None — later rules must never see or override it."""
    k = Kernel()
    calls: list[str] = []
    verdict = Ping(value=99)
    k.events.subscribe(Ping, lambda e: verdict, priority=1)
    k.events.subscribe(Ping, lambda e: calls.append("hijacker") or "other", priority=2)
    assert k.events.waterfall_sync(Ping()) is verdict
    assert calls == []


def test_waterfall_all_transform_chain_returns_final_event():
    """A transform-only chain is a normalization pipeline — its work must
    reach the caller, not vanish into a None."""
    k = Kernel()
    k.events.subscribe(Ping, lambda e: Transform(replace(e, value=e.value + 1)), priority=1)
    k.events.subscribe(Ping, lambda e: Transform(replace(e, value=e.value * 2)), priority=2)
    assert k.events.waterfall_sync(Ping(value=1)) == Ping(value=4)


def test_waterfall_transform_wrong_event_type_is_loud():
    k = Kernel()
    k.events.subscribe(Ping, lambda e: Transform(Other()))
    with pytest.raises(TypeError):
        k.events.waterfall_sync(Ping())


async def test_waterfall_error_policy():
    """Errors propagate by default (a gate that silently skips a deny rule is
    a hole); propagate_errors=False downgrades to log-and-continue."""
    k = Kernel()

    def boom(e):
        raise RuntimeError("broken rule")

    k.events.subscribe(Ping, boom, priority=1)
    k.events.subscribe(Ping, lambda e: "fallback", priority=2)
    with pytest.raises(RuntimeError):
        await k.events.waterfall(Ping())
    with pytest.raises(RuntimeError):
        k.events.waterfall_sync(Ping())
    assert await k.events.waterfall(Ping(), propagate_errors=False) == "fallback"
    assert k.events.waterfall_sync(Ping(), propagate_errors=False) == "fallback"


def test_waterfall_sync_rejects_async_handler_loudly():
    """Unlike notify, silently dropping a RULE would change the verdict."""
    k = Kernel()

    async def rule(e):
        return "verdict"

    k.events.subscribe(Ping, rule)
    with pytest.raises(TypeError):
        k.events.waterfall_sync(Ping())


async def test_collect_gathers_non_none_in_dispatch_order():
    k = Kernel()

    async def probe_b(e):
        return "b"

    k.events.subscribe(Ping, lambda e: "c", priority=3)
    k.events.subscribe(Ping, lambda e: None, priority=2)  # abstain stays out
    k.events.subscribe(Ping, probe_b, priority=1)
    assert await k.events.collect(Ping()) == ["b", "c"]


def test_collect_error_isolation_default_and_loud_option():
    """A broken probe abstains by default (StateProbeSpec contract); callers
    needing completeness can opt into propagation."""
    k = Kernel()

    def boom(e):
        raise RuntimeError("broken probe")

    k.events.subscribe(Ping, boom, priority=1)
    k.events.subscribe(Ping, lambda e: "ok", priority=2)
    assert k.events.collect_sync(Ping()) == ["ok"]
    with pytest.raises(RuntimeError):
        k.events.collect_sync(Ping(), propagate_errors=True)


def test_mode_filtering_restricts_handler_to_declared_modes():
    """A notify-only debug tap must never become an accidental waterfall
    verdict for the same event type."""
    k = Kernel()
    seen: list[str] = []
    k.events.subscribe(Ping, lambda e: seen.append("tap") or "would-be-verdict", modes={"notify"})
    ping = Ping()
    assert k.events.waterfall_sync(ping) is ping  # no verdict → event back
    k.events.notify_sync(ping)
    assert seen == ["tap"]


def test_subscribe_rejects_unknown_mode():
    k = Kernel()
    with pytest.raises(ValueError):
        k.events.subscribe(Ping, lambda e: None, modes={"bail"})


def test_events_match_exact_type_only():
    """No MRO walk: a Ping subscriber does not hear Other, and vice versa."""
    k = Kernel()
    seen: list[str] = []
    k.events.subscribe(Ping, lambda e: seen.append("ping"))
    k.events.notify_sync(Other())
    assert seen == []


def test_subclass_event_bypasses_base_handlers_but_logs_debug(caplog):
    """The documented sharp edge: Urgent(Ping) never reaches Ping handlers,
    and dispatch debug-logs the miss once per type so it is diagnosable."""

    @dataclass(frozen=True)
    class Urgent(Ping):
        pass

    k = Kernel()
    seen: list[str] = []
    k.events.subscribe(Ping, lambda e: seen.append("base"))
    with caplog.at_level(logging.DEBUG, logger="conductor.kernel"):
        k.events.notify_sync(Urgent())
        k.events.notify_sync(Urgent())  # warn-once: second dispatch stays quiet
    assert seen == []
    hits = [r for r in caplog.records if "Urgent" in r.message and "Ping" in r.message]
    assert len(hits) == 1


def test_subscriptions_introspection_lists_dispatch_order():
    k = Kernel()

    def a(e): ...

    def b(e): ...

    k.events.subscribe(Ping, b, priority=5, modes={"notify"})
    k.events.subscribe(Ping, a, priority=1)
    subs = k.events.subscriptions(Ping)
    assert [s.handler for s in subs] == [a, b]
    assert subs[1].modes == frozenset({"notify"}) and subs[0].modes is None
    assert k.events.subscriptions(Other) == ()  # unknown type never raises


def test_subscription_dispose_detaches_handler():
    k = Kernel()
    seen: list[str] = []
    eff = k.events.subscribe(Ping, lambda e: seen.append("x"))
    eff.dispose()
    k.events.notify_sync(Ping())
    assert seen == []


# ── effects: dispose semantics ───────────────────────────────────────────────


def test_effect_double_dispose_runs_undo_once():
    calls: list[int] = []
    eff = Effect(lambda: calls.append(1))
    eff.dispose()
    eff.dispose()
    assert calls == [1] and eff.disposed


def test_composite_disposes_children_lifo():
    order: list[str] = []
    comp = CompositeEffect()
    comp.add(Effect(lambda: order.append("first")))
    comp.add(Effect(lambda: order.append("second")))
    comp.dispose()
    assert order == ["second", "first"]  # override unwinds before its base


def test_composite_double_dispose_and_late_add():
    order: list[str] = []
    comp = CompositeEffect()
    comp.add(Effect(lambda: order.append("a")))
    comp.dispose()
    comp.dispose()
    assert order == ["a"]
    late = comp.add(Effect(lambda: order.append("late")))  # after teardown → immediate
    assert late.disposed and order == ["a", "late"]


def test_composite_child_error_does_not_abort_teardown():
    order: list[str] = []
    comp = CompositeEffect()
    comp.add(Effect(lambda: order.append("inner")))
    comp.add(Effect(lambda: (_ for _ in ()).throw(RuntimeError("bad undo"))))
    comp.dispose()
    assert order == ["inner"]


async def test_async_undo_requires_dispose_async_and_stays_recoverable():
    """Sync dispose() on an async undo must refuse loudly WITHOUT consuming
    the handle — dispose_async afterwards still runs the teardown."""
    done: list[str] = []

    async def undo():
        done.append("async")

    eff = Effect(undo)
    with pytest.raises(TypeError):
        eff.dispose()
    assert not eff.disposed and done == []  # refused, not leaked-as-disposed
    await eff.dispose_async()
    await eff.dispose_async()  # idempotent
    assert done == ["async"] and eff.disposed


async def test_dispose_async_handles_sync_undo_too():
    done: list[str] = []
    eff = Effect(lambda: done.append("sync"))
    await eff.dispose_async()
    assert done == ["sync"] and eff.disposed


async def test_composite_dispose_async_unwinds_mixed_scope_lifo():
    """One teardown path for a mixed sync/async plugin footprint."""
    order: list[str] = []

    async def a_undo():
        order.append("async")

    comp = CompositeEffect()
    comp.add(Effect(lambda: order.append("sync")))
    comp.add(Effect(a_undo))
    await comp.dispose_async()
    assert order == ["async", "sync"] and comp.disposed
    comp.dispose()  # double-dispose across flavors stays a no-op
    assert order == ["async", "sync"]


# ── kernel: scope + reset ────────────────────────────────────────────────────


def test_scope_tears_down_a_plugins_whole_footprint():
    """One plugin, one scope: service provider + event handler both unwind
    with a single dispose — the unload primitive end to end."""
    k = Kernel()
    seen: list[str] = []
    scope = k.scope()
    scope.add(k.services.register_provider(GREETER, "plugin-impl"))
    scope.add(k.events.subscribe(Ping, lambda e: seen.append("x")))
    assert k.services.get(GREETER) == "plugin-impl"
    scope.dispose()
    with pytest.raises(UnknownServiceError):
        k.services.get(GREETER)
    k.events.notify_sync(Ping())
    assert seen == []


def test_default_kernel_reset_preserves_identity_and_tolerates_stale_effects():
    """Modules import the singleton once; reset must blank it in place, and an
    Effect issued before the reset must dispose as a no-op afterward."""
    before = default_kernel
    eff = default_kernel.services.register_provider(GREETER, "x")
    sub = default_kernel.events.subscribe(Ping, lambda e: None)
    reset_kernel()
    assert default_kernel is before
    with pytest.raises(UnknownServiceError):
        default_kernel.services.get(GREETER)
    eff.dispose()  # stale handles: harmless no-ops, no raise
    sub.dispose()
    # behaviorally the same LIVE bus: a fresh subscription on the old
    # reference still receives dispatches after the reset
    seen: list[str] = []
    default_kernel.events.subscribe(Ping, lambda e: seen.append("alive"))
    default_kernel.events.notify_sync(Ping())
    assert seen == ["alive"]
