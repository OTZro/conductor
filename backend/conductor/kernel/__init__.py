"""conductor.kernel — the microkernel seam (M1: pure new code, not yet wired).

The Service / Typed-Event / Effect trio from the Cordis reference, sized for
conductor: everything a plugin registers is an Effect (reversible), every
capability is a ServiceDefinition (exactly one active provider), every hook
point is a typed Event with a declared dispatch mode. Existing Specs and the
lanes rule chains map onto this surface in M2+; nothing imports it yet.

Public API (import from here, not the submodules — this line is the contract):

- ``kernel`` / ``Kernel`` / ``reset_kernel`` — the shared instance + lifecycle
- ``ServiceDefinition`` / ``ProviderInfo`` + the ServiceError family — the
  capability seam
- ``Event`` / ``Transform`` / ``EventBus`` / ``SubscriptionInfo`` / ``MODES``
  — the typed event bus
- ``Effect`` / ``CompositeEffect`` — reversible registration handles
"""

from __future__ import annotations

from .effects import CompositeEffect, Effect
from .events import MODES, Event, EventBus, SubscriptionInfo, Transform
from .kernel import Kernel, kernel, reset_kernel
from .services import (
    DuplicateProviderError,
    ProviderInfo,
    ServiceDefinition,
    ServiceError,
    ServiceRegistry,
    UnknownServiceError,
)

__all__ = [
    "MODES", "CompositeEffect", "DuplicateProviderError", "Effect", "Event",
    "EventBus", "Kernel", "ProviderInfo", "ServiceDefinition", "ServiceError",
    "ServiceRegistry", "SubscriptionInfo", "Transform", "UnknownServiceError",
    "kernel", "reset_kernel",
]
