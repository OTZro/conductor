/**
 * Unit tests for the FE kernel — a plain node-executable script, wired into
 * nothing (the repo has no FE test runner; see kernel/README.md for the
 * decision). Run with:
 *
 *   cd frontend && npx tsc src/kernel/kernel.test.ts \
 *     --outDir /tmp/conductor-kernel-tests --module commonjs \
 *     --moduleResolution node --target es2020 --strict --skipLibCheck \
 *   && node /tmp/conductor-kernel-tests/kernel/kernel.test.js
 *
 * No test framework, no node type deps: a tiny inline runner that throws (→
 * nonzero exit) if any case fails. Type-checked by the project's normal
 * `tsc --noEmit` (it lives under src/) but never imported by app code, so it
 * ships in no bundle.
 */

import {
  CollectionRegistry,
  CompositeEffect,
  DuplicateProviderError,
  Effect,
  EventBus,
  Kernel,
  ServiceRegistry,
  Transform,
  UnknownServiceError,
} from "./kernel";
import { SURFACE_CARD, SURFACE_OVERLAY, kernel as appKernel } from "./seams";

// ── micro-runner ────────────────────────────────────────────────────────────

const failures: string[] = [];
let passed = 0;

function test(name: string, fn: () => void): void {
  try {
    fn();
    passed += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    failures.push(`${name}: ${err instanceof Error ? err.message : String(err)}`);
    console.error(`FAIL  ${name}: ${err}`);
  }
}

function assert(cond: unknown, msg: string): asserts cond {
  if (!cond) throw new Error(msg);
}

function assertThrows(fn: () => void, ctor: new (...a: never[]) => Error, msg: string): Error {
  try {
    fn();
  } catch (err) {
    assert(err instanceof ctor, `${msg} — threw ${String(err)} instead of ${ctor.name}`);
    return err;
  }
  throw new Error(`${msg} — did not throw`);
}

// ── Effect / CompositeEffect ────────────────────────────────────────────────

test("Effect: dispose runs undo exactly once (idempotent)", () => {
  let n = 0;
  const e = new Effect(() => (n += 1));
  assert(!e.disposed, "fresh effect not disposed");
  e.dispose();
  e.dispose();
  assert(n === 1, `undo ran ${n} times, want 1`);
  assert(e.disposed, "disposed flag set");
});

test("CompositeEffect: LIFO teardown, error isolation, add-after-dispose", () => {
  const order: string[] = [];
  const scope = new CompositeEffect();
  scope.add(new Effect(() => order.push("a")));
  scope.add(
    new Effect(() => {
      order.push("boom");
      throw new Error("broken child");
    }),
  );
  scope.add(new Effect(() => order.push("c")));
  scope.dispose();
  assert(order.join(",") === "c,boom,a", `LIFO with isolation, got ${order.join(",")}`);
  let late = 0;
  scope.add(new Effect(() => (late += 1)));
  assert(late === 1, "add() on a disposed scope disposes the child immediately");
});

// ── ServiceRegistry ─────────────────────────────────────────────────────────

test("ServiceRegistry: unknown service is loud and names what exists", () => {
  const reg = new ServiceRegistry();
  reg.register("known.service", 1);
  const err = assertThrows(() => reg.get("nope"), UnknownServiceError, "get on unknown service");
  assert(err.message.includes('"nope"'), "error names the missing service");
  assert(err.message.includes("known.service"), "error lists known services");
});

test("ServiceRegistry: higher priority wins; dispose reactivates the previous", () => {
  const reg = new ServiceRegistry();
  reg.register("svc", "core", { priority: 0 });
  const eff = reg.register("svc", "plugin", { priority: 10 });
  assert(reg.get("svc") === "plugin", "higher priority is active");
  eff.dispose();
  assert(reg.get("svc") === "core", "disposing the override restores the default");
  eff.dispose(); // idempotent, still fine
  assert(reg.get("svc") === "core", "double-dispose is a no-op");
});

test("ServiceRegistry: equal priority anywhere requires override", () => {
  const reg = new ServiceRegistry();
  reg.register("svc", "core", { priority: 0 });
  reg.register("svc", "top", { priority: 10 });
  // a tie BURIED under the top provider is still rejected (it would surface
  // nondeterministically when "top" disposes)
  assertThrows(
    () => reg.register("svc", "rival", { priority: 0 }),
    DuplicateProviderError,
    "buried equal-priority registration without override",
  );
  reg.register("svc", "rival", { priority: 0, override: true });
  assert(reg.get("svc") === "top", "override at a lower priority does not change the active");
});

test("ServiceRegistry: override tie resolves by registration order (later seq wins)", () => {
  const reg = new ServiceRegistry();
  reg.register("svc", "first", { priority: 5 });
  reg.register("svc", "second", { priority: 5, override: true });
  assert(reg.get("svc") === "second", "explicit override wins the tie");
});

test("ServiceRegistry: providers() lists active-first; reset() leaves stale effects harmless", () => {
  const reg = new ServiceRegistry();
  const eff = reg.register("svc", "core", { priority: 0 });
  reg.register("svc", "over", { priority: 1 });
  const rows = reg.providers("svc");
  assert(rows.length === 2 && rows[0].provider === "over" && rows[0].active, "active first");
  assert(!rows[1].active, "shadowed row not active");
  assert(reg.providers("ghost").length === 0, "unknown service introspects to empty, no throw");
  reg.reset();
  eff.dispose(); // must not throw on rows cleared by reset
  assertThrows(() => reg.get("svc"), UnknownServiceError, "reset emptied the registry");
});

// ── EventBus ────────────────────────────────────────────────────────────────

type Ev = {
  ping: { n: number };
  gate: { value: number };
};

test("EventBus.notify: priority order (lower first), seq tiebreak, error isolation", () => {
  const bus = new EventBus<Ev>();
  const seen: string[] = [];
  bus.subscribe("ping", () => seen.push("late"), { priority: 10 });
  bus.subscribe("ping", () => {
    seen.push("boom");
    throw new Error("observer broke");
  }, { priority: 0 });
  bus.subscribe("ping", () => seen.push("tie2"), { priority: 0 });
  bus.notify("ping", { n: 1 });
  assert(seen.join(",") === "boom,tie2,late", `dispatch order, got ${seen.join(",")}`);
});

test("EventBus.notify: unsubscribe via effect", () => {
  const bus = new EventBus<Ev>();
  let n = 0;
  const eff = bus.subscribe("ping", () => (n += 1));
  bus.notify("ping", { n: 0 });
  eff.dispose();
  bus.notify("ping", { n: 0 });
  assert(n === 1, "disposed handler no longer dispatched");
});

test("EventBus.waterfallSync: first verdict short-circuits", () => {
  const bus = new EventBus<Ev>();
  const ran: string[] = [];
  bus.subscribe("gate", () => {
    ran.push("abstain");
    return null;
  }, { priority: 1 });
  bus.subscribe("gate", () => {
    ran.push("verdict");
    return "DENY";
  }, { priority: 2 });
  bus.subscribe("gate", () => {
    ran.push("never");
    return "ALLOW";
  }, { priority: 3 });
  const out = bus.waterfallSync("gate", { value: 1 });
  assert(out === "DENY", "first non-null verdict returned");
  assert(ran.join(",") === "abstain,verdict", "later rules never ran");
});

test("EventBus.waterfallSync: Transform replaces payload; all-abstain returns final payload", () => {
  const bus = new EventBus<Ev>();
  bus.subscribe("gate", (e) => new Transform({ value: e.value + 1 }), { priority: 1 });
  bus.subscribe("gate", (e) => new Transform({ value: e.value * 10 }), { priority: 2 });
  const out = bus.waterfallSync("gate", { value: 1 }) as Ev["gate"];
  assert(out.value === 20, `transform chain reached the caller, got ${out.value}`);
});

test("EventBus.waterfallSync: errors propagate by default, isolation on request", () => {
  const bus = new EventBus<Ev>();
  bus.subscribe("gate", () => {
    throw new Error("broken rule");
  }, { priority: 1 });
  bus.subscribe("gate", () => "VERDICT", { priority: 2 });
  assertThrows(() => bus.waterfallSync("gate", { value: 1 }), Error, "default propagates");
  const out = bus.waterfallSync("gate", { value: 1 }, { propagateErrors: false });
  assert(out === "VERDICT", "isolated mode skips the broken rule and continues");
});

// ── Kernel + the surface.card seam ──────────────────────────────────────────

test("Kernel: scope funnels registrations; reset preserves identity", () => {
  const k = new Kernel<Ev>();
  const scope = k.scope();
  scope.add(k.services.register("svc", "core"));
  let n = 0;
  scope.add(k.events.subscribe("ping", () => (n += 1)));
  assert(k.services.get("svc") === "core", "provider active");
  k.events.notify("ping", { n: 0 });
  scope.dispose(); // one dispose unwinds the whole footprint
  assertThrows(() => k.services.get("svc"), UnknownServiceError, "provider unwound");
  k.events.notify("ping", { n: 0 });
  assert(n === 1, "subscription unwound");
  const services = k.services;
  k.reset();
  assert(k.services === services, "reset keeps the same registry object");
});

// THE seam-usability proof (M6 §4c): an alternate provider for surface.card
// registered at higher priority — in a TEST only, never in app code — wins
// resolution; disposing it restores the default. Runs against the REAL app
// kernel and the REAL "surface.card" service id from seams.ts. The true core
// provider (CardDetail) lives in bootstrap.ts, which imports React app code
// and can't load under bare node, so a stand-in "core" value plays its role
// at the same priority 0 the bootstrap uses — the registry mechanics under
// test are identical.
test("surface.card seam: higher-priority alternate wins, dispose restores the default", () => {
  const core = { name: "core CardDetail flow (stand-in)" };
  const alternate = { name: "dummy alternate card surface" };
  const coreEff = appKernel.services.register(SURFACE_CARD, core, { priority: 0 });
  try {
    assert(appKernel.services.get(SURFACE_CARD) === core, "default resolves before override");

    const scope = appKernel.scope();
    scope.add(appKernel.services.register(SURFACE_CARD, alternate, { priority: 100 }));
    assert(
      appKernel.services.get(SURFACE_CARD) === alternate,
      "alternate provider at higher priority is the active surface",
    );
    const stack = appKernel.services.providers(SURFACE_CARD);
    assert(stack.length === 2 && stack[0].provider === alternate && !stack[1].active,
      "core stays registered underneath, shadowed");

    scope.dispose(); // the plugin-unload story
    assert(
      appKernel.services.get(SURFACE_CARD) === core,
      "disposing the alternate restores the default provider",
    );
  } finally {
    coreEff.dispose();
  }
});

// ── CollectionRegistry (the surface.overlay seam's registry) ────────────────

test("CollectionRegistry: empty collection is a valid default (all → [])", () => {
  const col = new CollectionRegistry();
  assert(Array.isArray(col.all("anything")) && col.all("anything").length === 0,
    "unknown/empty collection yields [] — never throws");
});

test("CollectionRegistry: ALL items apply, registration order, no duplicate check", () => {
  const col = new CollectionRegistry();
  col.add("overlays", "a");
  col.add("overlays", "b");
  col.add("overlays", "a"); // same value twice is fine — no exactly-one semantics
  assert(col.all("overlays").join(",") === "a,b,a", "registration order, duplicates allowed");
});

test("CollectionRegistry: dispose withdraws exactly that item; reset clears in place", () => {
  const col = new CollectionRegistry();
  const ea = col.add("ov", "a");
  col.add("ov", "b");
  ea.dispose();
  assert(col.all("ov").join(",") === "b", "disposed item withdrawn, sibling stays");
  ea.dispose(); // idempotent
  col.reset();
  assert(col.all("ov").length === 0, "reset empties the collection");
});

test("Kernel: carries a collections registry; reset covers it", () => {
  const k = new Kernel();
  k.collections.add("ov", 1);
  k.reset();
  assert(k.collections.all("ov").length === 0, "kernel.reset() also resets collections");
});

// ── getBase (the wrap-the-default story) ────────────────────────────────────

test("ServiceRegistry.getBase: resolves the priority-0 default under an override", () => {
  const reg = new ServiceRegistry();
  reg.register("svc", "core", { priority: 0 });
  const eff = reg.register("svc", "plugin", { priority: 100 });
  assert(reg.get("svc") === "plugin", "active is the override");
  assert(reg.getBase("svc") === "core", "getBase still hands back the core default");
  eff.dispose();
  assert(reg.getBase("svc") === "core", "base unchanged after the override unloads");
  assertThrows(
    () => new ServiceRegistry().getBase("svc"),
    UnknownServiceError,
    "getBase on an unprovided service is loud like get()",
  );
});

// The overlay seam's app-level default: ZERO providers on the real app kernel
// (core contributes none), so App's render site maps over [] and renders
// nothing — today's behavior, proven here — and a plugin item unloads cleanly.
test("surface.overlay seam: zero providers by default; a plugin item lists and unloads", () => {
  assert(
    appKernel.collections.all(SURFACE_OVERLAY).length === 0,
    "core registers no overlay provider",
  );
  const scope = appKernel.scope();
  const overlay = { name: "dummy overlay surface" };
  scope.add(appKernel.collections.add(SURFACE_OVERLAY, overlay));
  assert(
    appKernel.collections.all(SURFACE_OVERLAY)[0] === overlay,
    "registered overlay item is returned to the render site",
  );
  scope.dispose();
  assert(
    appKernel.collections.all(SURFACE_OVERLAY).length === 0,
    "unload restores the render-nothing default",
  );
});

// ── the bootstrap-guard regression (2026-09-09 cardwindows incident) ────────
// A plugin package evaluated during the plugins/registry import registers its
// provider BEFORE bootstrap's body runs. Bootstrap's idempotence guard must
// key on the priority-0 row it adds — an emptiness check would skip the core
// registration entirely: no drawer (the plugin renders null) and getBase()
// resolving the plugin itself, which is what shipped.
test("bootstrap guard: plugin registering first must not suppress the core default", () => {
  const reg = new ServiceRegistry();
  reg.register("surface.x", "plugin-adopter", { priority: 100 }); // plugin evals first
  // the OLD guard's condition — proves the bug it caused:
  if (reg.providers("surface.x").length === 0) reg.register("surface.x", "core", { priority: 0 });
  assert(reg.getBase("surface.x") === "plugin-adopter",
    "old emptiness guard leaves the plugin as its own base — the incident");
  // the FIXED guard's condition — core registers regardless of eval order:
  if (!reg.providers("surface.x").some((p) => p.priority === 0))
    reg.register("surface.x", "core", { priority: 0 });
  assert(reg.get("surface.x") === "plugin-adopter", "plugin stays the active surface");
  assert(reg.getBase("surface.x") === "core", "core default is the base again");
  // and it stays idempotent (the HMR story the guard existed for):
  if (!reg.providers("surface.x").some((p) => p.priority === 0))
    reg.register("surface.x", "core", { priority: 0 }); // must not run → no duplicate throw
  assert(reg.providers("surface.x").length === 2, "re-eval registers nothing twice");
});

// ── summary ─────────────────────────────────────────────────────────────────

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length > 0) {
  throw new Error(`kernel tests failed:\n  ${failures.join("\n  ")}`);
}
