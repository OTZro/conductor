"""The capability seam: Service Definition / Provider / Consumer.

A seam is three roles (per the Cordis reference glossary): a ServiceDefinition
declares a replaceable capability as a named contract; one or more PROVIDERS
implement it; CONSUMERS resolve it by definition and never import an
implementation. Swap the provider (local shell → sandbox, jira cloud → on-prem)
and every consumer follows at once — no per-consumer fork. This is the shape
the four hand-registered sources in main.py eventually collapse into: a
"source" ServiceDefinition with jira/github/slack as first-party
providers (M2+; nothing is integrated here).

Exactly ONE provider is active per service at any time. The registry's whole
job is making that selection explicit — priority + override — instead of
last-import-wins. Shadowed providers stay registered underneath, so disposing
the active one's Effect reactivates the previous: that is the plugin-unload
story (a plugin overrides a capability; unloading it restores the original).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass

from .effects import Effect


class ServiceError(Exception):
    """Base for capability-seam failures, so callers can catch the family."""


class UnknownServiceError(ServiceError, LookupError):
    """Resolution of a service nobody provides. Deliberately loud (never a
    silent None) — the current plugin system's failure mode is a silently
    empty ``getattr``; this seam replaces that with an error naming the
    missing capability and what IS available."""


class DuplicateProviderError(ServiceError):
    """Two providers tied at a priority without an explicit winner. Raised at
    REGISTRATION time, not resolution time, so a misconfigured plugin fails
    at load rather than nondeterministically at first use."""


@dataclass(frozen=True)
class ServiceDefinition[T]:
    """Declares a replaceable capability. This object IS the contract handle:
    consumers import the definition (cheap, no implementation code) and resolve
    through it. ``T`` is the provider's protocol/type, so ``registry.get(SHELL)``
    type-checks as the contract without any cast at call sites. ``contract`` is
    prose documentation of the protocol — kept on the definition because the
    definition is the one artifact both provider and consumer must read."""

    name: str  # registry key, e.g. "conductor.source"
    version: str = "1"  # bumped on breaking contract change; informational in M1
    contract: str = ""  # human description of what a provider must implement


@dataclass(frozen=True)
class _Registration:
    priority: int
    seq: int  # global registration counter — the deterministic tiebreak
    provider: object


@dataclass(frozen=True)
class ProviderInfo:
    """Read-only row of ServiceRegistry.providers — for status surfaces and
    loader-side validation. Carries ``active`` explicitly so callers never
    re-derive the selection rule."""

    service: str
    priority: int
    seq: int
    provider: object
    active: bool


class ServiceRegistry:
    """Provider bookkeeping + resolution. Active provider = highest (priority,
    seq); an equal-priority challenger must pass ``override=True``, so a later
    seq at the top only ever exists on purpose."""

    def __init__(self) -> None:
        self._rows: dict[str, list[_Registration]] = {}
        self._seq = 0

    @staticmethod
    def _key(service: ServiceDefinition | str) -> str:
        return service.name if isinstance(service, ServiceDefinition) else str(service)

    def register_provider[T](
        self,
        service: ServiceDefinition[T] | str,
        provider: T,
        *,
        priority: int = 0,
        override: bool = False,
    ) -> Effect:
        """Offer PROVIDER for SERVICE; returns the Effect that withdraws it.

        Higher ``priority`` wins outright (an explicit ranking needs no flag).
        A priority already present ANYWHERE in the stack requires
        ``override=True`` — not just a top tie: a tie buried under a higher
        provider would surface when that provider disposes and then resolve
        by registration order, the exact last-import-wins this registry
        exists to kill. Rejected at registration time, while someone can
        still fix it. Distinct lower priorities slot in silently underneath."""
        name = self._key(service)
        rows = self._rows.setdefault(name, [])
        if not override and any(r.priority == priority for r in rows):
            raise DuplicateProviderError(
                f"service {name!r} already has a provider at priority {priority}; "
                f"pass override=True (or a distinct priority) to disambiguate"
            )
        self._seq += 1
        row = _Registration(priority=priority, seq=self._seq, provider=provider)
        rows.append(row)

        def _undo() -> None:
            # Tolerant removal: the row may already be gone after a reset();
            # a stale Effect disposing post-reset must stay a no-op.
            try:
                rows.remove(row)
            except ValueError:
                pass

        return Effect(_undo)

    def get[T](self, service: ServiceDefinition[T] | str) -> T:
        """Resolve the single active provider. Raises UnknownServiceError when
        nobody provides it — listing what IS registered, because "which name
        did I misspell" is the entire debugging session otherwise."""
        name = self._key(service)
        rows = self._rows.get(name)
        if not rows:
            known = sorted(k for k, v in self._rows.items() if v)
            raise UnknownServiceError(
                f"no provider registered for service {name!r} (known services: {known})"
            )
        return max(rows, key=lambda r: (r.priority, r.seq)).provider  # type: ignore[return-value]

    def providers(self, service: ServiceDefinition | str) -> tuple[ProviderInfo, ...]:
        """Read-only provider stack, active first then descending (priority,
        seq) — the order they would activate as rows above dispose. Unknown
        service → empty tuple: introspection never raises (get()'s job)."""
        name = self._key(service)
        rows = sorted(
            self._rows.get(name, []), key=lambda r: (r.priority, r.seq), reverse=True
        )
        return tuple(
            ProviderInfo(
                service=name, priority=r.priority, seq=r.seq,
                provider=r.provider, active=(i == 0),
            )
            for i, r in enumerate(rows)
        )

    def inject(self, **deps: ServiceDefinition | str) -> Callable:
        """Consumer-side dependency declaration: decorate a function and each
        keyword becomes an auto-filled argument resolved from the registry.

        Resolution happens at CALL time, not decoration time, so provider and
        consumer plugins can load in any order — the reason inject-style DI
        exists instead of "import the implementation". Explicitly passed
        kwargs win over injection, which is how tests hand in fakes without
        touching the registry. Works unchanged on async functions (the wrapper
        resolves synchronously, then returns the coroutine)."""

        def deco(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                for kw, svc in deps.items():
                    if kw not in kwargs:
                        kwargs[kw] = self.get(svc)
                return fn(*args, **kwargs)

            return wrapper

        return deco

    def reset(self) -> None:
        """Drop every registration — test-isolation helper (see Kernel.reset).
        Clears the row lists IN PLACE so Effects created before the reset hold
        references to the same (now empty) lists and dispose harmlessly."""
        for rows in self._rows.values():
            rows.clear()
        self._rows.clear()
