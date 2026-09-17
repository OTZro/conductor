"""Reversible registration handles — the kernel's lifecycle primitive.

Every registration the kernel hands out (a service provider, an event handler)
comes back as an Effect: a handle whose ``dispose()`` undoes exactly that one
registration. This is what makes a plugin unloadable without a process restart:
teardown is a precise undo held by the runtime, not "flip a disabled flag,
restart, and pray no global state leaked" (the current toggle story).
CompositeEffect groups a plugin's registrations so its whole footprint unwinds
as one unit — the hot-reload primitive the Cordis reference calls "registrations
are effects that unwind when their plugin unloads".
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable

log = logging.getLogger("conductor.kernel")


class Effect:
    """One reversible registration.

    ``dispose()`` runs the undo exactly once — double-dispose is a documented
    no-op, because teardown paths overlap in practice (a plugin disposing its
    own handle inside a scope that is itself being torn down) and the second
    caller must neither explode nor double-undo. The undo closure is dropped
    after it runs so a long-lived Effect handle can't pin registry rows alive.

    The undo may be async (teardown that must await — closing a client,
    cancelling a task): use ``dispose_async()``. Plain ``dispose()`` on an
    async undo raises TypeError and leaves the Effect UNdisposed — silently
    skipping an async teardown would leak the resource the Effect guards.
    """

    __slots__ = ("_undo", "_disposed")

    def __init__(self, undo: Callable[[], None] | Callable[[], Awaitable[None]]) -> None:
        self._undo: Callable | None = undo
        self._disposed = False

    @property
    def disposed(self) -> bool:
        return self._disposed

    def dispose(self) -> None:
        """Undo the registration. Idempotent. Raises TypeError on an async
        undo — without marking disposed, so dispose_async can still run."""
        if self._disposed:
            return
        if inspect.iscoroutinefunction(self._undo):
            raise TypeError("undo is async; dispose with dispose_async()")
        self._disposed = True
        undo, self._undo = self._undo, None
        if undo is not None:
            result = undo()
            if inspect.isawaitable(result):
                # A sync-LOOKING undo produced an awaitable (a lambda closing
                # over an async fn, an async __call__, …) that the coroutine-
                # function check above can't see. Silently dropping it would
                # skip the real teardown, so restore the handle UNdisposed and
                # raise — dispose_async() can then run it for real.
                self._undo, self._disposed = undo, False
                if inspect.iscoroutine(result):
                    result.close()  # don't leave a never-awaited coroutine warning
                raise TypeError("undo returned an awaitable; dispose with dispose_async()")

    async def dispose_async(self) -> None:
        """Awaiting flavor of dispose(): handles sync AND async undos — one
        teardown path for a mixed scope. Same idempotency contract."""
        if self._disposed:
            return
        self._disposed = True
        undo, self._undo = self._undo, None
        if undo is not None:
            result = undo()
            if inspect.isawaitable(result):
                await result


class CompositeEffect(Effect):
    """A bag of Effects torn down as one unit — the per-plugin lifetime scope.

    Children dispose in LIFO order (reverse of registration) because stacked
    registrations tend to depend on earlier ones — an override provider should
    unwind before the base provider it shadows. A child that raises is logged
    and skipped, never aborting the rest of the teardown: one broken plugin
    registration must not leave its siblings registered forever. ``add()``
    after the composite is disposed disposes the child immediately — a plugin
    racing its own teardown must not leak a live registration.

    A scope holding any async-undo child must be torn down with
    ``dispose_async()``; sync ``dispose()`` on such a child hits its TypeError,
    logged and skipped — a leak, but the log line names the handle to fix.
    """

    __slots__ = ("_children",)

    def __init__(self, children: Iterable[Effect] = ()) -> None:
        # Bound method is safe to hand to super() here: it is only invoked at
        # dispose time, well after _children exists.
        super().__init__(self._dispose_children)
        self._children: list[Effect] = list(children)

    def add[E: Effect](self, effect: E) -> E:
        """Adopt EFFECT into this scope; returns it so registration call sites
        can wrap in place: ``scope.add(bus.subscribe(...))``."""
        if self.disposed:
            effect.dispose()
        else:
            self._children.append(effect)
        return effect

    async def dispose_async(self) -> None:
        """LIFO teardown awaiting each child's dispose_async — the one path
        unwinding a mixed sync/async scope completely. Same error isolation."""
        if self.disposed:
            return
        self._disposed = True
        self._undo = None  # neutralize the sync path; children drain here
        while self._children:
            child = self._children.pop()
            try:
                await child.dispose_async()
            except Exception as exc:  # noqa: BLE001 — teardown must run to completion
                log.warning("[kernel] effect dispose failed: %s", exc)

    def _dispose_children(self) -> None:
        while self._children:
            child = self._children.pop()
            try:
                child.dispose()
            except Exception as exc:  # noqa: BLE001 — teardown must run to completion
                log.warning("[kernel] effect dispose failed: %s", exc)
