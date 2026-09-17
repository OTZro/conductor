# FE kernel (M6)

A tiny frontend mirror of `backend/conductor/kernel` — the Service / Typed-Event /
Effect trio — plus the first three **surface seams** with the app's existing
implementations registered as the DEFAULT providers. **Nothing visible changed**:
the seams resolve to the exact same component references the call sites used to
import directly, no alternate providers ship, and no runtime loading exists.
Seam usability is proven by tests only.

## Files

| File | Role |
|---|---|
| `kernel.ts` | Pure, dependency-free kernel: `ServiceRegistry`, `EventBus` (`notify` + `waterfallSync`), `Effect`/`CompositeEffect`, `Kernel`. Unit-testable logic, no React, no I/O. |
| `seams.ts` | The shared app `kernel` instance, the seam ids + contracts, the `AppEvents` map, and typed resolver helpers. Runtime-light (type-only imports of app code). |
| `bootstrap.ts` | Registers the core default providers at priority 0. Side-effect imported once from `main.tsx`, before the first render. |
| `kernel.test.ts` | Node-executable unit tests (see "Tests" below). Never imported by app code. |

## Kernel semantics (mirrors the backend)

- **Services** — exactly ONE active provider per service: highest `(priority,
  registration order)`. An equal priority anywhere in the stack requires
  `override: true` (loud `DuplicateProviderError` at registration time,
  otherwise). Resolving an unprovided service throws `UnknownServiceError`
  naming what IS registered. Shadowed providers stay underneath; disposing the
  active one reactivates the previous — the plugin-unload story.
- **Events** — name-keyed, payload-typed via the `AppEvents` map. `notify` is
  fan-out observation (a broken handler is logged and skipped, lower priority
  dispatches first); `waterfallSync` is an ordered rule chain (first
  non-nullish, non-`Transform` return is the verdict and short-circuits;
  `new Transform(p)` replaces the payload for later handlers; all-abstain
  returns the final payload; errors propagate by default).
- **Effects** — every registration returns an `Effect` whose `dispose()`
  undoes exactly that registration (idempotent). `kernel.scope()` returns a
  `CompositeEffect`: funnel a plugin's registrations through `scope.add(...)`
  and one `dispose()` unwinds the whole footprint, LIFO, error-isolated.

## The seams

| Service id | Contract (see `seams.ts`) | Core default (bootstrap) | Consumer |
|---|---|---|---|
| `surface.card` | `CardSurfaceComponent` — how the selected card renders | `CardDetail` (the right-drawer flow) | `App.tsx` resolves `cardSurface()` and renders it with the exact props it used to pass `CardDetail` |
| `surface.terminal` | `TerminalSurfaceComponent` — how a card terminal host renders | `TerminalView` (embedded ttyd flow) | `CardDetail.tsx`'s terminal call site resolves `terminalSurface()` |
| `registry.layouts` | `LayoutRegistries` — the three plugin-layout maps | The `import.meta.glob`-merged maps from `plugins/registry.tsx` (the glob stays the authoring format; it just feeds the registry) | `PluginPanel`, `PluginMenuBar`, `PluginCardWidgets`, `TerminalView` resolve `layoutRegistries()` |

Because each default provider IS the pre-existing component/object (same
reference, same props, same mount position, same z-index/animation), the
rendered tree is pixel-for-pixel what the direct imports produced — the
indirection is mechanical only.

`TerminalsPanel` intentionally keeps its direct `TerminalView` import: the seam
covers the *card* terminal host (the CardDetail call site), per the M6 scope.

Two later additions on top of M6:

- **`surface.overlay`** — a **collection** seam (`kernel.collections`, see
  `CollectionRegistry`): ALL registered providers render, in registration
  order, with no priority/duplicate semantics — App maps `surfaceOverlays()`
  near its modals layer. Zero providers (the core default — bootstrap
  registers none) renders nothing. Overlays own the **z-[65]** band: above the
  header (55) and the fullscreen terminal (60), under the modals (70).
- **`getBase()`** on `ServiceRegistry` (+ `baseCardSurface()` helper) —
  resolves the *lowest*-(priority, seq) provider, i.e. the core default, even
  while a plugin's alternate is active. For alternates that WRAP the default
  surface instead of replacing its rendering (e.g. a windowing surface hosting
  the real CardDetail inside its own chrome).

## Events dispatched (interception points; nothing consumes them yet)

- `card.open` `{ id }` — a card became the selected card (fires on card→card
  switches too, without an intervening close)
- `card.close` `{ id }` — the drawer closed; `id` is the card that was open
- `view.change` `{ view, prev }` — the main view switched

Dispatched from `App.tsx` in effects that observe the existing state
transitions after they settle — every path (click, Esc, deep-link, nav rail,
`g` chords) is covered without touching any call site.

## How a plugin WOULD provide an alternate (documentation only — none exists)

```ts
import { SURFACE_CARD, kernel } from "./kernel/seams";
import type { CardSurfaceComponent } from "./kernel/seams";

const scope = kernel.scope();
// higher priority than core's 0 → becomes the active card surface everywhere
scope.add(
  kernel.services.register<CardSurfaceComponent>(SURFACE_CARD, MyCardSurface, {
    priority: 100,
  }),
);
// observe/intercept the moments a surface would care about
scope.add(kernel.events.subscribe("card.open", ({ id }) => prefetch(id)));

// unload: one dispose unwinds everything and the core drawer is back
scope.dispose();
```

The provider must satisfy the seam's contract type (`CardSurfaceProps` etc. in
`seams.ts`) — the contract, not the core component, is the seam's artifact.

## Tests

The repo has no FE test-runner config, so `kernel.test.ts` is a plain
node-executable script wired into nothing (no framework, no new deps; it is
type-checked by the project's normal `tsc --noEmit` but ships in no bundle):

```sh
cd frontend && npx tsc src/kernel/kernel.test.ts \
  --outDir /tmp/conductor-kernel-tests --module commonjs \
  --moduleResolution node --target es2020 --strict --skipLibCheck \
&& node /tmp/conductor-kernel-tests/kernel/kernel.test.js
```

It covers the registry/bus/effect semantics above, and the seam proof: a dummy
alternate provider for `surface.card` registered at higher priority (in the
test only) wins resolution, and disposing it restores the default.
