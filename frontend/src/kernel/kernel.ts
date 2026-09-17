/**
 * The FE microkernel — a tiny TypeScript mirror of backend/conductor/kernel
 * (services.py / events.py / effects.py), sized for the frontend:
 *
 * - ServiceRegistry — the capability seam. Exactly ONE provider is active per
 *   service; selection is explicit (priority + override), never
 *   last-import-wins. Shadowed providers stay registered underneath, so
 *   disposing the active one reactivates the previous — the plugin-unload
 *   story.
 * - EventBus — typed events with declared dispatch modes. `notify` (fan-out
 *   observation, errors isolated) and `waterfallSync` (ordered rule chain,
 *   first non-null verdict short-circuits, `Transform` replaces the payload)
 *   are sufficient on the FE; the backend's async forms and `collect` have no
 *   FE call site yet.
 * - Effect / CompositeEffect — reversible registration handles. Everything a
 *   plugin registers comes back as an Effect whose dispose() undoes exactly
 *   that registration; a CompositeEffect (scope) unwinds a whole footprint
 *   LIFO as one unit.
 *
 * Pure logic, zero dependencies, no React, no I/O — constructing a Kernel
 * builds empty registries and nothing else, so importing this module is
 * always safe and cheap. The app's shared instance lives in ./seams.ts (the
 * FE analogue of the backend's module-level `kernel` singleton).
 */

// ── errors ──────────────────────────────────────────────────────────────────

/** Base for capability-seam failures, so callers can catch the family. */
export class ServiceError extends Error {}

/** Resolution of a service nobody provides. Deliberately loud (never a silent
 * undefined) — the error names the missing capability and what IS known. */
export class UnknownServiceError extends ServiceError {}

/** Two providers tied at a priority without an explicit winner. Raised at
 * REGISTRATION time, not resolution time, so a misconfigured plugin fails at
 * load rather than nondeterministically at first use. */
export class DuplicateProviderError extends ServiceError {}

// ── effects ─────────────────────────────────────────────────────────────────

/** One reversible registration. dispose() runs the undo exactly once —
 * double-dispose is a documented no-op (teardown paths overlap in practice).
 * The undo closure is dropped after it runs so a long-lived handle can't pin
 * registry rows alive. FE undos are always sync (no dispose_async needed). */
export class Effect {
  private undo: (() => void) | null;
  private isDisposed = false;

  constructor(undo: () => void) {
    this.undo = undo;
  }

  get disposed(): boolean {
    return this.isDisposed;
  }

  dispose(): void {
    if (this.isDisposed) return;
    this.isDisposed = true;
    const undo = this.undo;
    this.undo = null;
    undo?.();
  }
}

/** A bag of Effects torn down as one unit — the per-plugin lifetime scope.
 * Children dispose in LIFO order (an override provider unwinds before the
 * base it shadows). A child that throws is logged and skipped, never aborting
 * the rest of the teardown. add() after disposal disposes the child
 * immediately — a plugin racing its own teardown must not leak. */
export class CompositeEffect extends Effect {
  private children: Effect[];

  constructor(children: Iterable<Effect> = []) {
    // The closure reads this.children lazily (at dispose time), well after
    // the field is assigned below — safe despite running through super().
    super(() => this.disposeChildren());
    this.children = [...children];
  }

  add<E extends Effect>(effect: E): E {
    if (this.disposed) effect.dispose();
    else this.children.push(effect);
    return effect;
  }

  private disposeChildren(): void {
    while (this.children.length) {
      const child = this.children.pop()!;
      try {
        child.dispose();
      } catch (err) {
        // teardown must run to completion — one broken registration must not
        // leave its siblings registered forever
        console.warn("[kernel] effect dispose failed:", err);
      }
    }
  }
}

// ── services ────────────────────────────────────────────────────────────────

type Registration = {
  priority: number;
  seq: number; // global registration counter — the deterministic tiebreak
  provider: unknown;
};

/** Read-only row of ServiceRegistry.providers — for introspection and tests.
 * Carries `active` explicitly so callers never re-derive the selection rule. */
export type ProviderInfo = {
  service: string;
  priority: number;
  seq: number;
  provider: unknown;
  active: boolean;
};

/** Provider bookkeeping + resolution. Active provider = highest (priority,
 * seq); an equal-priority challenger must pass `override: true`, so a later
 * seq at the top only ever exists on purpose. */
export class ServiceRegistry {
  private rows = new Map<string, Registration[]>();
  private seq = 0;

  /** Offer PROVIDER for SERVICE; returns the Effect that withdraws it.
   *
   * Higher `priority` wins outright. A priority already present ANYWHERE in
   * the stack requires `override: true` — not just a top tie: a tie buried
   * under a higher provider would surface when that provider disposes and
   * then resolve by registration order, the exact last-import-wins this
   * registry exists to kill. Distinct lower priorities slot in silently. */
  register<T>(
    service: string,
    provider: T,
    opts: { priority?: number; override?: boolean } = {},
  ): Effect {
    const { priority = 0, override = false } = opts;
    let rows = this.rows.get(service);
    if (!rows) this.rows.set(service, (rows = []));
    if (!override && rows.some((r) => r.priority === priority)) {
      throw new DuplicateProviderError(
        `service "${service}" already has a provider at priority ${priority}; ` +
          `pass override: true (or a distinct priority) to disambiguate`,
      );
    }
    const row: Registration = { priority, seq: ++this.seq, provider };
    rows.push(row);
    const list = rows;
    return new Effect(() => {
      // Tolerant removal: the row may already be gone after a reset(); a
      // stale Effect disposing post-reset must stay a no-op.
      const i = list.indexOf(row);
      if (i >= 0) list.splice(i, 1);
    });
  }

  /** Resolve the single active provider. Throws UnknownServiceError when
   * nobody provides it — listing what IS registered. */
  get<T>(service: string): T {
    const rows = this.rows.get(service);
    if (!rows || rows.length === 0) {
      const known = [...this.rows.entries()]
        .filter(([, v]) => v.length > 0)
        .map(([k]) => k)
        .sort();
      throw new UnknownServiceError(
        `no provider registered for service "${service}" (known services: [${known.join(", ")}])`,
      );
    }
    let best = rows[0];
    for (const r of rows) {
      if (r.priority > best.priority || (r.priority === best.priority && r.seq > best.seq)) best = r;
    }
    return best.provider as T;
  }

  /** Read-only provider stack, active first then descending (priority, seq)
   * — the order they would activate as rows above dispose. Unknown service →
   * empty array: introspection never throws (get()'s job). */
  providers(service: string): ProviderInfo[] {
    const rows = [...(this.rows.get(service) ?? [])].sort(
      (a, b) => b.priority - a.priority || b.seq - a.seq,
    );
    return rows.map((r, i) => ({
      service,
      priority: r.priority,
      seq: r.seq,
      provider: r.provider,
      active: i === 0,
    }));
  }

  /** Resolve the BASE provider — lowest (priority, seq), i.e. the core default
   * bootstrap registered at priority 0 even while a plugin shadows it. For
   * hosts that WRAP the default surface (an alternate provider that still
   * renders the core component inside its own chrome). Throws like get() when
   * nobody provides the service. */
  getBase<T>(service: string): T {
    const rows = this.rows.get(service);
    if (!rows || rows.length === 0) return this.get<T>(service); // reuse the loud error
    let base = rows[0];
    for (const r of rows) {
      if (r.priority < base.priority || (r.priority === base.priority && r.seq < base.seq)) base = r;
    }
    return base.provider as T;
  }

  /** Drop every registration — test-isolation helper. Clears the row lists IN
   * PLACE so Effects created before the reset dispose harmlessly. */
  reset(): void {
    for (const rows of this.rows.values()) rows.length = 0;
    this.rows.clear();
  }
}

// ── collections ─────────────────────────────────────────────────────────────

/** The many-providers counterpart to ServiceRegistry: ALL registered items
 * render/apply, in registration order — no priority, no active-one selection,
 * no duplicate check (two plugins contributing to the same collection is the
 * point). Zero items is a valid state (all() → []), which is what keeps a
 * collection-backed render site invisible until a plugin shows up. Same
 * Effect-based unregistration as everything else in the kernel. */
export class CollectionRegistry {
  private rows = new Map<string, { seq: number; item: unknown }[]>();
  private seq = 0;

  /** Contribute ITEM to NAME; returns the Effect that withdraws it. */
  add<T>(name: string, item: T): Effect {
    let rows = this.rows.get(name);
    if (!rows) this.rows.set(name, (rows = []));
    const row = { seq: ++this.seq, item };
    rows.push(row);
    const list = rows;
    return new Effect(() => {
      const i = list.indexOf(row);
      if (i >= 0) list.splice(i, 1);
    });
  }

  /** Every item of NAME in registration order. Unknown name → [] (never
   * throws — an empty collection is the ordinary case, not an error). */
  all<T>(name: string): T[] {
    return (this.rows.get(name) ?? []).map((r) => r.item as T);
  }

  /** Drop every item — test-isolation helper. In-place clear so pre-reset
   * Effects dispose harmlessly. */
  reset(): void {
    for (const rows of this.rows.values()) rows.length = 0;
    this.rows.clear();
  }
}

// ── events ──────────────────────────────────────────────────────────────────

/** The app declares its event vocabulary as a name → payload map (see
 * seams.ts); the bus is generic over it so emit/subscribe both type-check. */
export type EventMap = Record<string, unknown>;

/** Explicit waterfall payload replacement: `return new Transform(next)` swaps
 * the payload for later handlers, chain continues. A wrapper on purpose —
 * bare returns are always verdicts, so a payload-shaped verdict can never be
 * misread as a transform and overrun by later rules. */
export class Transform<P> {
  constructor(readonly payload: P) {}
}

type Subscription = {
  priority: number;
  seq: number;
  handler: (payload: never) => unknown;
};

/** One subscription row, exposed read-only via subscriptions(). */
export type SubscriptionInfo = Readonly<Subscription>;

/** Subscription bookkeeping + the two dispatch loops the FE needs. Ordering
 * is deterministic everywhere: (priority, registration order), LOWER priority
 * first — "rule 1 beats rule 2" reads like an if-chain. Events are matched by
 * exact name — one name per hook point, greppable. */
export class EventBus<E extends EventMap = EventMap> {
  private subs = new Map<string, Subscription[]>();
  private seq = 0;

  /** Attach HANDLER to NAME; returns the Effect that detaches it. `priority`
   * orders handlers (lower runs first; ties break by registration order). */
  subscribe<K extends keyof E & string>(
    name: K,
    handler: (payload: E[K]) => unknown,
    opts: { priority?: number } = {},
  ): Effect {
    const { priority = 0 } = opts;
    let subs = this.subs.get(name);
    if (!subs) this.subs.set(name, (subs = []));
    const sub: Subscription = { priority, seq: ++this.seq, handler: handler as Subscription["handler"] };
    subs.push(sub);
    const list = subs;
    return new Effect(() => {
      const i = list.indexOf(sub);
      if (i >= 0) list.splice(i, 1);
    });
  }

  private ordered(name: string): Subscription[] {
    return [...(this.subs.get(name) ?? [])].sort(
      (a, b) => a.priority - b.priority || a.seq - b.seq,
    );
  }

  /** Fan PAYLOAD out to every handler. One handler's exception is logged and
   * the loop continues — observation must never break the emitter. */
  notify<K extends keyof E & string>(name: K, payload: E[K]): void {
    for (const sub of this.ordered(name)) {
      try {
        (sub.handler as (p: E[K]) => unknown)(payload);
      } catch (err) {
        console.warn(`[kernel] notify handler for "${name}" failed:`, err);
      }
    }
  }

  /** Run handlers in order until one returns a verdict (any non-nullish,
   * non-Transform value); returns that verdict, or the final (possibly
   * Transform-replaced) payload when every handler abstained — a
   * transform-only chain is a normalization pipeline and its work must reach
   * the caller. `propagateErrors: true` (default) lets a broken rule surface
   * — a gate that silently skips a deny rule is a hole, so isolation here is
   * opt-IN, the inverse of notify. */
  waterfallSync<K extends keyof E & string>(
    name: K,
    payload: E[K],
    opts: { propagateErrors?: boolean } = {},
  ): E[K] | unknown {
    const { propagateErrors = true } = opts;
    let current = payload;
    for (const sub of this.ordered(name)) {
      let result: unknown;
      try {
        result = (sub.handler as (p: E[K]) => unknown)(current);
      } catch (err) {
        if (propagateErrors) throw err;
        console.warn(`[kernel] waterfall handler for "${name}" failed:`, err);
        continue;
      }
      if (result === null || result === undefined) continue; // abstain
      if (result instanceof Transform) {
        current = result.payload as E[K];
        continue;
      }
      return result; // verdict — short-circuit
    }
    return current; // no verdict: hand back the final transformed payload
  }

  /** Read-only listing of an event's subscriptions in dispatch order.
   * Unknown name → empty array: introspection describes, it never throws. */
  subscriptions(name: keyof E & string): SubscriptionInfo[] {
    return this.ordered(name);
  }

  /** Drop every subscription — test-isolation helper. In-place clear so
   * pre-reset Effects dispose harmlessly. */
  reset(): void {
    for (const subs of this.subs.values()) subs.length = 0;
    this.subs.clear();
  }
}

// ── kernel ──────────────────────────────────────────────────────────────────

/** The registries as one unit, plus per-plugin scoping. One Kernel is the
 * whole "context" a plugin sees — the capability seam for what it can USE,
 * the event bus for what it can OBSERVE or GATE, and scopes so everything it
 * registered unwinds together at unload. */
export class Kernel<E extends EventMap = EventMap> {
  readonly services = new ServiceRegistry();
  readonly events = new EventBus<E>();
  readonly collections = new CollectionRegistry();

  /** A fresh teardown scope — one plugin, one scope. Funnel every register/
   * subscribe Effect through `scope.add(...)` and the plugin's entire
   * footprint unwinds with a single dispose(): the unload primitive. The
   * kernel does not track scopes; the caller owns the handle. */
  scope(): CompositeEffect {
    return new CompositeEffect();
  }

  /** Empty both registries IN PLACE. For tests: holders of this kernel keep
   * valid references (same object, now blank), and pre-reset Effects dispose
   * harmlessly. */
  reset(): void {
    this.services.reset();
    this.events.reset();
    this.collections.reset();
  }
}
