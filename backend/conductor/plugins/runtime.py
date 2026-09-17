"""Kernel-side plugin runtime — the M2 bridge from discovery onto the kernel.

Discovery (``plugins/__init__``) keeps its exact external surface (``PLUGINS``,
``PLUGIN_ROUTERS``, ``MODULES``); this module is the NEW half: after a module's
``PLUGIN`` dataclass is imported, it is registered into the kernel as a
:class:`~conductor.kernel.CompositeEffect` scope holding one reversible
registration per declared render slot and spec surface. As of M3 every core
consumption site resolves through these registries (``spec_rows`` /
``slot_spec``) with a ``PLUGINS`` read-through — the dataclass is just the
authoring format — and the scope makes a plugin's footprint precisely
unwindable (``dispose_plugin``), which is the seed of hot-unload.

Why this file lives under ``plugins/`` and not ``kernel/``: the kernel is the
generic mechanism (services / events / effects) and must not know conductor's
plugin domain — render slots, the ``Plugin`` dataclass, module naming. This
module owns that mapping, importing ``conductor.kernel`` and ``plugins.base``,
so the dependency arrow stays kernel ← plugins and no cycle forms.

Registry model: one :class:`SlotRegistry` per render slot (tab / card_widget /
menu_bar), keyed by plugin id, each row carrying byte-for-byte the payload
``Plugin.manifest()`` emits for that slot today (derived by CALLING
``manifest()``, so the two can never drift) plus the live spec object for M3+.
The three registries are themselves kernel services (``conductor.render.*``) —
resolved lazily so a test's ``reset_kernel()`` simply yields fresh empty
registries on next use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..kernel import CompositeEffect, Effect, ServiceDefinition, UnknownServiceError, kernel
from .base import RENDER_SLOTS

if TYPE_CHECKING:
    from .base import Plugin

#: One capability per render slot; the provider is that slot's SlotRegistry.
SLOT_SERVICES: dict[str, ServiceDefinition["SlotRegistry"]] = {
    slot: ServiceDefinition(
        name=f"conductor.render.{slot}",
        contract=f"SlotRegistry of every plugin's {slot} declaration, keyed by plugin id",
    )
    for slot in RENDER_SLOTS
}


@dataclass(frozen=True)
class SlotRegistration:
    """One plugin's declaration for one render slot.

    ``payload`` is exactly the dict ``Plugin.manifest()`` emits for this slot —
    the FE-facing data. ``spec`` is the live ``*Spec`` dataclass (carrying the
    provider callable etc.) for kernel-side consumers in later milestones."""

    plugin_id: str
    slot: str
    payload: dict
    spec: object


class SlotRegistry:
    """Insertion-ordered ``plugin_id -> SlotRegistration`` for one render slot.

    ``register`` returns the Effect that withdraws exactly that row, so a
    plugin's scope can unwind it. Re-registration under the same plugin id
    replaces the row (mirroring ``PLUGINS[plugin.id] = plugin`` last-wins in
    discovery); the replaced row's Effect then disposes as a no-op — undo only
    removes the row it put there."""

    def __init__(self, slot: str) -> None:
        self.slot = slot
        self._rows: dict[str, SlotRegistration] = {}

    def register(self, registration: SlotRegistration) -> Effect:
        self._rows[registration.plugin_id] = registration

        def _undo() -> None:
            if self._rows.get(registration.plugin_id) is registration:
                del self._rows[registration.plugin_id]

        return Effect(_undo)

    def get(self, plugin_id: str) -> SlotRegistration | None:
        return self._rows.get(plugin_id)

    def registrations(self) -> tuple[SlotRegistration, ...]:
        return tuple(self._rows.values())


def slot_registry(slot: str) -> SlotRegistry:
    """Resolve the SlotRegistry for SLOT, providing an empty one on first use
    (or after a test's ``reset_kernel()`` wiped the services)."""
    service = SLOT_SERVICES[slot]  # KeyError on an unknown slot, deliberately
    try:
        return kernel.services.get(service)
    except UnknownServiceError:
        registry = SlotRegistry(slot)
        kernel.services.register_provider(service, registry)
        return registry


def slot_spec(slot: str, plugin_id: str) -> object | None:
    """The live spec behind one plugin's render slot — the call surface for
    ``GET /api/plugins/{id}/tab`` and ``POST /api/plugins/{id}/card``.

    Read-through mirrors :func:`spec_rows`: a live ``PLUGINS`` entry is
    authoritative — the dataclass stays the authoring format, so dict-injected
    plugins (the test idiom) and plugins whose kernel registration failed
    degrade to legacy behavior instead of vanishing. A kernel row with no
    ``PLUGINS`` entry is a kernel-native registrant and serves its registered
    spec directly. None ⇒ nobody declares that slot under that id (404)."""
    from . import PLUGINS  # lazy — the package init imports this module

    plugin = PLUGINS.get(plugin_id)
    if plugin is not None:
        return getattr(plugin, slot)
    reg = slot_registry(slot).get(plugin_id)
    return reg.spec if reg is not None else None


#: The tuple-shaped ``Plugin`` fields migrated onto kernel registries in M3 (batch
#: 1: hooks…notifiers; batch 2: stages, lane_changes). Each is a "many registrants,
#: core iterates" surface — same shape as the render slots, so they get the same
#: treatment: one kernel-service registry per surface (NOT collect-mode events,
#: because these are standing declarations enumerated outside any event occurrence —
#: /api/status lists polls, the auth middleware needs hook paths before any request —
#: and last-wins/iteration-order semantics must mirror the ``PLUGINS`` dict, which a
#: registry states directly).
SPEC_FIELDS: tuple[str, ...] = (
    "hooks", "state_probes", "polls", "link_enrichers", "link_matchers", "notifiers",
    "stages", "lane_changes",
)

#: Scalar spec surfaces — ``Plugin`` fields holding one optional declaration (a
#: bare callable or None) rather than a tuple. Normalized to a 0/1-tuple by
#: ``_declared_specs`` so they ride the same SpecRegistry machinery.
SCALAR_SPEC_FIELDS: tuple[str, ...] = ("card_body",)


def _declared_specs(plugin: "Plugin", field: str) -> tuple:
    """PLUGIN's declarations for FIELD as a tuple — the one normalization point
    between the authoring format and the registries (scalar fields become a
    0/1-tuple; tuple fields pass through)."""
    value = getattr(plugin, field)
    if field in SCALAR_SPEC_FIELDS:
        return () if value is None else (value,)
    return tuple(value)


#: One capability per spec surface; the provider is that surface's SpecRegistry.
SPEC_SERVICES: dict[str, ServiceDefinition["SpecRegistry"]] = {
    field: ServiceDefinition(
        name=f"conductor.specs.{field}",
        contract=f"SpecRegistry of every plugin's {field} declarations, keyed by plugin id",
    )
    for field in SPEC_FIELDS + SCALAR_SPEC_FIELDS
}


@dataclass(frozen=True)
class SpecRegistration:
    """One registrant's declarations for one spec surface — the whole tuple, plugin-
    granular like a SlotRegistration, so re-registration is last-wins per plugin id."""

    plugin_id: str
    field: str
    specs: tuple


class SpecRegistry:
    """Insertion-ordered ``plugin_id -> SpecRegistration`` for one spec surface.

    Same contract as SlotRegistry: ``register`` returns the Effect that withdraws
    exactly that row; same-id re-registration replaces the row and inerts the old
    row's Effect."""

    def __init__(self, field: str) -> None:
        self.field = field
        self._rows: dict[str, SpecRegistration] = {}

    def register(self, registration: SpecRegistration) -> Effect:
        self._rows[registration.plugin_id] = registration

        def _undo() -> None:
            if self._rows.get(registration.plugin_id) is registration:
                del self._rows[registration.plugin_id]

        return Effect(_undo)

    def get(self, plugin_id: str) -> SpecRegistration | None:
        return self._rows.get(plugin_id)

    def registrations(self) -> tuple[SpecRegistration, ...]:
        return tuple(self._rows.values())


def spec_registry(field: str) -> SpecRegistry:
    """Resolve the SpecRegistry for FIELD, providing an empty one on first use
    (or after a test's ``reset_kernel()`` wiped the services)."""
    service = SPEC_SERVICES[field]  # KeyError on an unknown surface, deliberately
    try:
        return kernel.services.get(service)
    except UnknownServiceError:
        registry = SpecRegistry(field)
        kernel.services.register_provider(service, registry)
        return registry


def spec_rows(field: str) -> list[tuple[str, object]]:
    """The read surface core consumers iterate: every ``(plugin_id, spec)`` pair for
    FIELD, flattened, in DISCOVERY order — byte-identical to the legacy
    ``for plugin in PLUGINS.values()`` loops in every path, including the one where a
    plugin's kernel mirror registration failed (found in M3b review: iterating kernel
    rows first pushed such a plugin to the tail, flipping first-wins collisions).

    So the spine is ``PLUGINS`` in dict order — for each id the kernel row, when
    present, reads through to the live dataclass field (the ``Plugin`` dataclass
    stays the authoring format; the dict entry, not the boot-time snapshot, is
    authoritative). Kernel-NATIVE registrants — no dataclass, no ``PLUGINS`` entry —
    follow in registration order, serving their registered specs directly."""
    from . import PLUGINS  # lazy — the package init imports this module

    rows: list[tuple[str, object]] = []
    for key, plugin in PLUGINS.items():
        rows.extend((key, spec) for spec in _declared_specs(plugin, field))
    for reg in spec_registry(field).registrations():
        if reg.plugin_id in PLUGINS:
            continue  # served above, from the live dataclass
        rows.extend((reg.plugin_id, spec) for spec in reg.specs)
    return rows


# module (directory) name -> that plugin's kernel scope. Keyed by MODULE, like
# disabled_modules(), because the scope's lifetime follows the module.
_SCOPES: dict[str, CompositeEffect] = {}


def register_plugin(module: str, plugin: "Plugin") -> CompositeEffect:
    """Register PLUGIN's render-slot declarations into the kernel as one scope.

    Called by discovery right after a module's ``PLUGIN`` is imported. The
    payload per slot is taken from ``plugin.manifest()`` so the kernel carries
    the exact data the manifest endpoint emits. Registering a module that
    already holds a scope disposes the old scope first (last-wins, matching
    ``PLUGINS`` dict semantics)."""
    dispose_plugin(module)
    manifest = plugin.manifest()
    scope = kernel.scope()
    for slot in RENDER_SLOTS:
        payload = manifest[slot]
        if payload is None:
            continue
        scope.add(
            slot_registry(slot).register(
                SlotRegistration(
                    plugin_id=plugin.id,
                    slot=slot,
                    payload=payload,
                    spec=getattr(plugin, slot),
                )
            )
        )
    # Every spec surface gets a row — empty tuples included — so the scope is the
    # module's complete declaration snapshot and read-through stays uniform.
    for field in SPEC_FIELDS + SCALAR_SPEC_FIELDS:
        scope.add(
            spec_registry(field).register(
                SpecRegistration(
                    plugin_id=plugin.id, field=field, specs=_declared_specs(plugin, field)
                )
            )
        )
    _SCOPES[module] = scope
    return scope


def plugin_scope(module: str) -> CompositeEffect | None:
    """The kernel scope discovery built for MODULE, or None (not loaded /
    disabled / disposed)."""
    return _SCOPES.get(module)


def dispose_plugin(module: str) -> None:
    """Unwind MODULE's kernel scope — every slot registration it made is
    withdrawn. Idempotent: unknown or already-disposed modules are a no-op.
    Internal (tests-only for now): runtime disable stays restart-based; this
    is the teardown primitive future hot-unload builds on."""
    scope = _SCOPES.pop(module, None)
    if scope is not None:
        scope.dispose()
