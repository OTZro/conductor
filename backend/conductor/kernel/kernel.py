"""The Kernel: services + events + effects tied into one shared object.

One Kernel is the whole "context" a plugin sees — the capability seam for
what it can USE, the event bus for what it can OBSERVE or GATE, and scopes so
everything it registered unwinds together at unload. A module-level default
instance is exported because conductor's idiom is a shared module singleton
(``bus.publish`` in bus.py, ``PLUGINS`` in plugins/__init__.py) — kernel
consumers import ``kernel`` the same way. Constructing the Kernel builds empty
registries and nothing else: no I/O, no discovery, no import-time side
effects, so importing this package is always safe and cheap.
"""

from __future__ import annotations

from .effects import CompositeEffect
from .events import EventBus
from .services import ServiceRegistry


class Kernel:
    """The three registries as one unit, plus per-plugin scoping."""

    def __init__(self) -> None:
        self.services = ServiceRegistry()
        self.events = EventBus()

    def scope(self) -> CompositeEffect:
        """A fresh teardown scope — one plugin, one scope. Funnel every
        register_provider/subscribe Effect through ``scope.add(...)`` and the
        plugin's entire footprint unwinds with a single ``dispose()``: the
        unload/hot-reload primitive. The kernel does not track scopes; the
        caller (the future plugin loader) owns the handle, because ownership
        of a lifetime should sit with whoever decides when it ends."""
        return CompositeEffect()

    def reset(self) -> None:
        """Empty both registries IN PLACE. For tests: modules that already
        imported the default kernel keep valid references (same object, now
        blank), and pre-reset Effects dispose harmlessly — no fixture dance."""
        self.services.reset()
        self.events.reset()


#: The shared default kernel. Application code imports this one instance;
#: constructing private Kernels is for tests and embedding.
kernel = Kernel()


def reset_kernel() -> None:
    """Test helper: wipe the default kernel between tests without replacing
    the object (see Kernel.reset for why identity is preserved)."""
    kernel.reset()
