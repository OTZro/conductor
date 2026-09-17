"""Typed event bus with declared dispatch modes.

Replaces the Spec zoo (HookSpec / LaneChangeSpec / NotifySpec / StateProbeSpec
are each a hand-rolled, single-purpose dispatch loop) with ONE registration
mechanism whose dispatch MODE is part of the event's public contract, per the
Cordis reference:

- ``notify``    — fan-out observation; no return value; a broken handler is
                  logged and skipped, because observers must never break the
                  emitter (today's NotifySpec / LaneChangeSpec shape).
- ``waterfall`` — ordered rule chain; the first non-None VERDICT
                  short-circuits. The permission-gate and lanes shape: the 19
                  recompute_ball rules become 19 handlers at declared
                  priorities, rule 1 first. ``Transform(new_event)`` replaces
                  the payload for later handlers (no verdict); the wrapper is
                  deliberate — ANY bare non-None return, Events included, is
                  a verdict, so an Event-typed verdict can never be hijacked
                  into a transform. All-abstain returns the final (possibly
                  transformed) event, so transform-only chains keep their work.
- ``collect``   — every handler's non-None result, gathered in dispatch order
                  (today's StateProbeSpec / manifest-assembly shape).

Every mode has an async form (handlers may be coroutines) and a ``_sync``
fast path for hot pure-compute call sites like lane evaluation. Ordering is
deterministic everywhere: ``(priority, registration order)``, LOWER priority
first — "rule 1 beats rule 2" reads exactly like lanes.py's if-chain today.

Events are matched by EXACT type, no MRO walk: distinct hook points are
distinct classes, and exact matching keeps "who receives this?" answerable by
grep rather than by inheritance archaeology. The sharp edge: a SUBCLASS event
silently bypasses handlers subscribed to its base class — define one class per
hook point, not hierarchies. Dispatch debug-logs once per event type when a
subscribed ancestor exists, so the mistake is at least visible in logs.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .effects import Effect

log = logging.getLogger("conductor.kernel")
#: The dispatch-mode vocabulary. Sync and async forms are the SAME mode for
#: filtering — a handler cares about semantics, not the caller's coloring.
MODES = frozenset({"notify", "waterfall", "collect"})


class Event:
    """Marker base for kernel events. Subclass as a frozen dataclass (repo
    idiom — see plugins/base.py): waterfall transforms then return a NEW
    instance via ``dataclasses.replace`` instead of mutating a payload some
    earlier handler already read. Dispatch matches the event's EXACT class:
    subclassing another Event does NOT inherit its subscribers."""

    __slots__ = ()


class Transform:
    """Explicit waterfall payload replacement: ``return Transform(new_event)``
    swaps the event for later handlers, chain continues. A wrapper on purpose —
    bare returns are always verdicts, so an Event-typed verdict can never be
    misread as a transform and overrun by later rules."""

    __slots__ = ("event",)

    def __init__(self, event: Event) -> None:
        self.event = event


@dataclass(frozen=True)
class SubscriptionInfo:
    """One subscription — both the internal row and the introspection shape
    (EventBus.subscriptions), so the listing can never drift from dispatch."""

    priority: int
    seq: int
    handler: Callable[[Any], Any]
    modes: frozenset[str] | None  # None → all modes


class EventBus:
    """Subscription bookkeeping + the four dispatch loops."""

    def __init__(self) -> None:
        self._subs: dict[type[Event], list[SubscriptionInfo]] = {}
        self._seq = 0
        self._ancestor_checked: set[type[Event]] = set()  # warn-once per dispatched type

    def subscribe(
        self,
        event_type: type[Event],
        handler: Callable[[Any], Any],
        *,
        priority: int = 0,
        modes: Iterable[str] | None = None,
    ) -> Effect:
        """Attach HANDLER to EVENT_TYPE; returns the Effect that detaches it.

        ``priority`` orders handlers (lower runs first; ties break by
        registration order — deterministic, so two runs of the same plugin set
        always dispatch identically). ``modes`` optionally restricts the
        handler to specific dispatch modes — e.g. a debug tap subscribing with
        ``modes={"notify"}`` can observe an event that is also waterfall-
        dispatched elsewhere without ever becoming an accidental verdict.
        Matching is by EXACT event class: a base-class subscription does NOT
        receive subclass events (see module docstring)."""
        mode_set: frozenset[str] | None = None
        if modes is not None:
            mode_set = frozenset(modes)
            unknown = mode_set - MODES
            if unknown:
                raise ValueError(f"unknown dispatch mode(s) {sorted(unknown)}; valid: {sorted(MODES)}")
        subs = self._subs.setdefault(event_type, [])
        self._seq += 1
        sub = SubscriptionInfo(priority=priority, seq=self._seq, handler=handler, modes=mode_set)
        subs.append(sub)

        def _undo() -> None:
            try:
                subs.remove(sub)
            except ValueError:  # already gone (reset) — stale dispose is a no-op
                pass

        return Effect(_undo)

    def _ordered(self, event: Event, mode: str) -> list[SubscriptionInfo]:
        etype = type(event)
        if etype not in self._ancestor_checked:
            # Once per type: exact-match dispatch means base-class subscribers
            # silently miss subclass events — make that diagnosable from logs.
            # MRO walk against dict keys only; nothing on the hot path.
            self._ancestor_checked.add(etype)
            ancestors = [c.__name__ for c in etype.__mro__[1:] if c in self._subs]
            if ancestors:
                log.debug(
                    "[kernel] %s dispatches only to its exact type; handlers on "
                    "ancestor(s) %s will NOT run", etype.__name__, ancestors,
                )
        subs = self._subs.get(etype, [])
        return sorted(
            (s for s in subs if s.modes is None or mode in s.modes),
            key=lambda s: (s.priority, s.seq),
        )

    # ── notify: fan-out, errors isolated ─────────────────────────────────────

    async def notify(self, event: Event) -> None:
        """Fan EVENT out to every handler, awaiting coroutines. One handler's
        exception is logged and the loop continues — observation must never
        break the emitter (the emit_lane_change guarantee, generalized)."""
        for sub in self._ordered(event, "notify"):
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 — a handler must not break dispatch
                log.warning("[kernel] notify handler %r failed: %s", sub.handler, exc)

    def notify_sync(self, event: Event) -> None:
        """Sync fast path of notify. A coroutine-returning handler here is a
        wiring bug, but per notify's isolation contract it is logged and
        skipped (and the coroutine closed to silence the never-awaited
        warning), never raised."""
        for sub in self._ordered(event, "notify"):
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    result.close()
                    log.warning("[kernel] async handler %r skipped in notify_sync", sub.handler)
            except Exception as exc:  # noqa: BLE001
                log.warning("[kernel] notify handler %r failed: %s", sub.handler, exc)

    # ── waterfall: ordered rules, first verdict wins ─────────────────────────

    @staticmethod
    def _waterfall_step(event: Event, result: Any) -> tuple[Event, Any]:
        """Classify one handler's return: None → abstain, ``Transform(e)`` →
        payload replacement (chain continues), any other value → verdict."""
        if result is None:
            return event, None
        if isinstance(result, Transform):
            if type(result.event) is not type(event):
                raise TypeError(
                    f"waterfall Transform must carry {type(event).__name__}, "
                    f"got {type(result.event).__name__}"
                )
            return result.event, None
        return event, result

    async def waterfall(self, event: Event, *, propagate_errors: bool = True) -> Any:
        """Run handlers in order until one returns a verdict; returns that
        verdict, or the final (possibly Transform-replaced) event when every
        handler abstained — a transform-only chain is a normalization
        pipeline and its work must reach the caller. ``propagate_errors=True``
        (default) lets a broken rule surface — a gate that silently skips a
        deny rule is a security hole, so isolation here is opt-IN, the inverse
        of notify."""
        for sub in self._ordered(event, "waterfall"):
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001 — policy decides
                if propagate_errors:
                    raise
                log.warning("[kernel] waterfall handler %r failed: %s", sub.handler, exc)
                continue
            event, verdict = self._waterfall_step(event, result)
            if verdict is not None:
                return verdict
        return event  # no verdict: hand back the final transformed payload

    def waterfall_sync(self, event: Event, *, propagate_errors: bool = True) -> Any:
        """Sync fast path of waterfall — same contract (verdict, or the final
        event when all abstain) for hot pure-compute chains (lane rules run on
        every board read). A coroutine here is a contract violation and raises
        TypeError loudly: unlike notify, silently dropping a RULE would change
        the verdict."""
        for sub in self._ordered(event, "waterfall"):
            try:
                result = sub.handler(event)
            except Exception as exc:  # noqa: BLE001 — policy decides
                if propagate_errors:
                    raise
                log.warning("[kernel] waterfall handler %r failed: %s", sub.handler, exc)
                continue
            if inspect.isawaitable(result):  # outside the try: never eaten by policy
                result.close()
                raise TypeError(f"async handler {sub.handler!r} in waterfall_sync dispatch")
            event, verdict = self._waterfall_step(event, result)
            if verdict is not None:
                return verdict
        return event  # no verdict: hand back the final transformed payload

    # ── collect: gather every result ─────────────────────────────────────────

    async def collect(self, event: Event, *, propagate_errors: bool = False) -> list[Any]:
        """Gather every handler's non-None result, in dispatch order (None =
        abstain, same as waterfall — a probe with nothing to say stays out of
        the list). Errors default to isolated (a broken probe abstains, per
        StateProbeSpec's contract) but can be made loud for callers that need
        completeness over resilience."""
        results: list[Any] = []
        for sub in self._ordered(event, "collect"):
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001 — policy decides
                if propagate_errors:
                    raise
                log.warning("[kernel] collect handler %r failed: %s", sub.handler, exc)
                continue
            if result is not None:
                results.append(result)
        return results

    def collect_sync(self, event: Event, *, propagate_errors: bool = False) -> list[Any]:
        """Sync fast path of collect. A coroutine here raises TypeError (like
        waterfall_sync): a silently missing result corrupts the collection."""
        results: list[Any] = []
        for sub in self._ordered(event, "collect"):
            try:
                result = sub.handler(event)
            except Exception as exc:  # noqa: BLE001 — policy decides
                if propagate_errors:
                    raise
                log.warning("[kernel] collect handler %r failed: %s", sub.handler, exc)
                continue
            if inspect.isawaitable(result):  # outside the try: never eaten by policy
                result.close()
                raise TypeError(f"async handler {sub.handler!r} in collect_sync dispatch")
            if result is not None:
                results.append(result)
        return results

    def subscriptions(self, event_type: type[Event]) -> tuple[SubscriptionInfo, ...]:
        """Read-only listing of an event type's subscriptions in dispatch order
        (all modes) — for status surfaces advertising what is wired (the
        /api/status shape) and loader-side duplicate validation. Unknown type
        → empty tuple: introspection describes, it never raises."""
        subs = self._subs.get(event_type, [])
        return tuple(sorted(subs, key=lambda s: (s.priority, s.seq)))

    def reset(self) -> None:
        """Drop every subscription — test-isolation helper (see Kernel.reset).
        Clears subscription lists in place so pre-reset Effects dispose
        harmlessly."""
        for subs in self._subs.values():
            subs.clear()
        self._subs.clear()
        self._ancestor_checked.clear()
