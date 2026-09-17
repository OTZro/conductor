import { CancelledError, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { authMe, getBoardLanes, getCard, getPluginManifest, listDashboards } from "./api";
import type { AuthMe, TDashboard, TPluginManifest } from "./api";
import { AvatarMenu } from "./components/AvatarMenu";
import { Board, FALLBACK_LANES } from "./components/Board";
import { CreateCardModal } from "./components/CreateCardModal";
import { FilterPopover, type SortBy } from "./components/FilterPopover";
import { Login } from "./components/Login";
import { PluginMenuBar } from "./components/PluginMenuBar";
import { PluginPanel } from "./components/PluginPanel";
import { TerminalsPanel } from "./components/TerminalsPanel";
import { UsageBadge } from "./components/UsageBadge";
import { cardSurface, kernel, surfaceOverlays } from "./kernel/seams";
import { usePersisted } from "./persist";
import { useCards, useCreateManual, useWsInvalidation } from "./queries";
import { CardSkeleton } from "./ui/primitives";
import { useToast } from "./ui/toast";
import type { BoardLane, Card } from "./types";

/** identical contents → keep the previous array, so a poll that reports no change
 *  doesn't re-render the whole board on its heartbeat */
const sameList = (a: string[], b: string[]) =>
  a.length === b.length && a.every((v, i) => v === b[i]);

// Built-in card sources — always offered in the filter (even with zero cards right now)
// so they can be pre-toggled. Plugin/custom sources are discovered dynamically from the
// cards actually on the board (see `sources` below), never hardcoded here.
const BUILTIN_SOURCES: { origin: string; label: string }[] = [
  { origin: "jira", label: "Jira" },
  { origin: "github", label: "PR" },
  { origin: "slack", label: "Slack" },
  { origin: "manual", label: "manual" },
];

const NAV = [
  { id: "board", icon: "▦", label: "Board" },
  { id: "terminals", icon: "▢", label: "Term" },
] as const;

export default function App() {
  const qc = useQueryClient();
  const { toast } = useToast();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // a card opened by id that isn't in the board list (snoozed/dismissed cards are
  // filtered out of /api/cards, but "open task" from a live terminal must still
  // open them). Fetched on demand below so the detail sheet has something to show.
  const [extraCard, setExtraCard] = useState<Card | null>(null);
  // shared with CardDetail so the content area can cede exactly the drawer's width
  // (desktop) instead of the drawer overlaying the terminal/board.
  const [drawerWidth, setDrawerWidth] = usePersisted<number>("conductor.drawerWidth", 720);
  const [connected, setConnected] = useState(false);
  // custom personal boards from the LOCAL ~/.conductor/dashboards.json (per-user, not
  // in the repo) — each renders a tab showing manual cards tagged with its id.
  const [dashboards, setDashboards] = useState<TDashboard[]>([]);
  const [plugins, setPlugins] = useState<TPluginManifest[]>([]); // discovered plugin manifests
  const [lanes, setLanes] = useState<BoardLane[]>(FALLBACK_LANES); // registry-driven lane set
  const [stale, setStale] = useState<string[]>([]); // degraded pollers (name + why)
  const [query, setQuery] = useState("");
  // persisted filter prefs (§3E.4) — don't reset on reload. We persist the HIDDEN set
  // (origins toggled OFF), not the enabled set: default empty ⇒ every source shows, and a
  // source that appears later (a plugin/custom source) is visible automatically instead of
  // being mistaken for "toggled off" — the old enabled-set model couldn't tell those apart.
  const [hiddenArr, setHiddenArr] = usePersisted<string[]>("conductor.hiddenSources", []);
  const hiddenSources = useMemo(() => new Set(hiddenArr), [hiddenArr]);
  const setHidden = useCallback(
    (fn: (prev: Set<string>) => Set<string>) => setHiddenArr([...fn(new Set(hiddenArr))]),
    [hiddenArr, setHiddenArr],
  );
  const [sortBy, setSortBy] = usePersisted<SortBy>("conductor.sort", "updated");
  const [view, setView] = useState<string>( // "board" | custom-dashboard-id | "orchestrator" | "terminals" | "monitor"
    () => (new URLSearchParams(location.search).get("view") as any) || "board",
  );
  const [showHidden, setShowHidden] = usePersisted("conductor.showHidden", false);
  const [showCreate, setShowCreate] = useState(false);
  const [me, setMe] = useState<AuthMe | null>(null);
  const [authState, setAuthState] = useState<"loading" | "anon" | "authed" | "off">("loading");
  // ?auth-setup shows the login screen even while auth is off, so a passkey can
  // be registered BEFORE flipping CONDUCTOR_AUTH_ENABLED=true (lockout safety).
  const [setupMode, setSetupMode] = useState(
    () => new URLSearchParams(window.location.search).has("auth-setup"),
  );
  const createManualMut = useCreateManual();
  const searchRef = useRef<HTMLInputElement>(null);

  const refreshAuth = useCallback(async () => {
    const m = await authMe(); // never throws — old backend w/o auth endpoints → auth off
    setMe(m);
    setAuthState(m.auth_enabled ? (m.authenticated ? "authed" : "anon") : "off");
  }, []);

  useEffect(() => {
    refreshAuth();
  }, [refreshAuth]);

  useEffect(() => {
    listDashboards().then(setDashboards).catch(() => setDashboards([]));
    getPluginManifest().then(setPlugins).catch(() => setPlugins([]));
    getBoardLanes().then(setLanes).catch(() => {}); // fallback: Board's builtin set
  }, []);

  useEffect(() => {
    // a gated fetch 401'd (session expired mid-use) → swap in the login screen
    const onUnauthorized = () => setAuthState((s) => (s === "authed" ? "anon" : s));
    window.addEventListener("conductor:unauthorized", onUnauthorized);
    return () => window.removeEventListener("conductor:unauthorized", onUnauthorized);
  }, []);

  // gate board polling + WS on auth: no fetch/reconnect spam against 401/4401
  const authOk = authState === "authed" || authState === "off";

  const onLoggedIn = useCallback(async () => {
    setSetupMode(false);
    if (new URLSearchParams(window.location.search).has("auth-setup")) {
      const url = new URL(window.location.href);
      url.searchParams.delete("auth-setup");
      window.history.replaceState(null, "", url.toString());
    }
    await refreshAuth();
  }, [refreshAuth]);

  // data layer (§3E.1): one query, WS-invalidated. Mutations live in queries.ts and
  // invalidate the cache themselves — no caller needs to remember to reload.
  const { data: cards = [], isLoading } = useCards(showHidden, authOk);
  const reload = useCallback(() => {
    qc.invalidateQueries({ queryKey: ["cards"] });
  }, [qc]);
  // The header ↻ button: same invalidation, but awaited so it can spin while the refetch
  // is in flight and toast when it lands — `reload` above stays fire-and-forget for its
  // programmatic callers (onChanged), which must not toast on every change.
  const [refreshing, setRefreshing] = useState(false);
  const manualRefresh = useCallback(async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      // throwOnError: invalidateQueries otherwise swallows a failed refetch and resolves,
      // so the success toast would fire even when the board didn't reload — make a rejected
      // refetch reach the catch.
      await qc.invalidateQueries({ queryKey: ["cards"] }, { throwOnError: true });
      toast("Board refreshed", { tone: "ok" });
    } catch (err) {
      // A concurrent invalidation (a WS card.upsert, the visibilitychange handler) can
      // cancel our refetch and reject with a silent CancelledError while the board still
      // reloaded fresh — not a failure. Only a real error gets the failure toast (kept
      // generic: it also fires on a 401 session-expiry, which isn't a connection problem).
      if (err instanceof CancelledError) toast("Board refreshed", { tone: "ok" });
      else toast("Refresh failed", { tone: "err" });
    } finally {
      setRefreshing(false);
    }
  }, [qc, toast, refreshing]);
  useWsInvalidation(authOk, setConnected);

  // deep-link (§3E.3): reflect view + open card (by external_id, shareable) in the
  // URL, and restore them on reload / back-forward. A notification links straight
  // to ?card=PROJ-1234 → the sheet opens on that job.
  useEffect(() => {
    const sel = cards.find((c) => c.id === selectedId);
    const p = new URLSearchParams();
    if (view !== "board") p.set("view", view);
    if (sel) p.set("card", sel.external_id);
    const qs = p.toString();
    window.history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
  }, [view, selectedId, cards]);

  useEffect(() => {
    if (selectedId) return;
    const ext = new URLSearchParams(location.search).get("card");
    if (ext) setSelectedId(cards.find((c) => c.external_id === ext)?.id ?? null);
  }, [cards, selectedId]);

  // kernel events (M6): announce card/view transitions on the bus — pure
  // additions observed AFTER the state settles, so every existing transition
  // path (click, Esc, deep-link, nav, `g` chords) is covered without touching
  // any call site. Nothing consumes these yet; they are the interception
  // points a plugin would subscribe to (see kernel/README.md).
  const prevCardRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = prevCardRef.current;
    if (selectedId === prev) return;
    prevCardRef.current = selectedId;
    if (selectedId) kernel.events.notify("card.open", { id: selectedId });
    else if (prev) kernel.events.notify("card.close", { id: prev });
  }, [selectedId]);
  const prevViewRef = useRef(view);
  useEffect(() => {
    const prev = prevViewRef.current;
    if (view === prev) return;
    prevViewRef.current = view;
    kernel.events.notify("view.change", { view, prev });
  }, [view]);

  // fallback fetch: if a card is selected but not in the board list (it's snoozed
  // or dismissed, so /api/cards omits it), pull it directly so "open task" from the
  // Terminals panel still opens the detail instead of silently doing nothing.
  useEffect(() => {
    if (!selectedId || cards.some((c) => c.id === selectedId)) {
      setExtraCard(null);
      return;
    }
    let alive = true;
    getCard(selectedId)
      .then((c) => alive && setExtraCard(c))
      .catch(() => alive && setExtraCard(null));
    return () => {
      alive = false;
    };
  }, [selectedId, cards]);

  // source health: a poller is degraded when its last success is older than
  // 3× its cadence (min 2min) or its latest run errored — turns the dot amber.
  useEffect(() => {
    if (!authOk) return;
    const check = async () => {
      try {
        const r = await fetch("/api/status");
        // A 5xx is not "all sources healthy". The server is up, so `connected` stays
        // green; only the health view is gone. Returning early would leave `stale` at
        // its last-known-good value — and the identity-preserving setStale below makes
        // that value stickier by design — so the dot would read green and titled
        // "live · all sources healthy" while the probe is dead. Say it instead.
        if (!r.ok) {
          const bad = [`status probe: HTTP ${r.status} — source health unknown`];
          setStale((prev) => (sameList(prev, bad) ? prev : bad));
          return;
        }
        const d = (await r.json()) as {
          pollers: Record<
            string,
            { age_s: number | null; error: string | null; interval_s: number | null }
          >;
        };
        const bad: string[] = [];
        for (const [name, p] of Object.entries(d.pollers || {})) {
          // interval_s === null marks a one-shot boot task (e.g. ttyd-reap): it never
          // runs again by design, so age-based staleness would flag it forever. Only a
          // real recorded error is worth surfacing for those.
          if (p.interval_s == null) {
            if (p.error) bad.push(`${name}: ${p.error}`);
            continue;
          }
          const limit = Math.max(3 * (p.interval_s || 60), 120);
          if (p.age_s == null || p.age_s > limit) bad.push(`${name}: no successful poll yet, or stale for ${Math.round((p.age_s || 0) / 60)} min`);
          else if (p.error) bad.push(`${name}: ${p.error}`);
        }
        // same contents -> same array, or this 30s heartbeat re-renders the board
        // (and every card in it) forever while reporting nothing new
        setStale((prev) => (sameList(prev, bad) ? prev : bad));
      } catch {
        /* status endpoint unreachable → the connected dot already covers it */
      }
    };
    check();
    const id = setInterval(check, 30000);
    return () => clearInterval(id);
  }, [authOk]);

  // keyboard model (§3C.2): global shortcuts, disabled while typing or when a
  // terminal owns focus (its own keys must pass through). `g` then b/t/r/m switches
  // view; `/` search; `n` new; Esc closes the sheet.
  useEffect(() => {
    let pendingG = false;
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement;
      const typing =
        el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el?.tagName === "IFRAME";
      if (e.key === "Escape") {
        setSelectedId(null);
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (pendingG) {
        pendingG = false;
        const map: Record<string, string> = { b: "board", t: "terminals" };
        for (const p of plugins) if (p.tab && p.id[0] && !map[p.id[0]]) map[p.id[0]] = p.id; // e.g. g s → sandbox
        if (map[e.key]) setView(map[e.key]);
        return;
      }
      if (e.key === "g") pendingG = true;
      else if (e.key === "/") {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "n") setShowCreate(true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [plugins]);

  // The source-filter's option list: built-ins (always) + every OTHER origin actually
  // present on the board — i.e. plugin/custom sources (an agent-team source, …), labelled from their
  // plugin manifest. So a source appears in the filter the moment it has a card, with no
  // hardcoding. Plugin sources sorted by manifest order (then name) for stability.
  const sources = useMemo(() => {
    const seen = new Set<string>();
    const out: { origin: string; label: string }[] = [];
    for (const b of BUILTIN_SOURCES) {
      out.push(b);
      seen.add(b.origin);
    }
    const extras = [...new Set(cards.map((c) => c.origin))].filter((o) => o && !seen.has(o));
    extras.sort((a, b) => {
      const oa = plugins.find((p) => p.id === a)?.order ?? 999;
      const ob = plugins.find((p) => p.id === b)?.order ?? 999;
      return oa - ob || a.localeCompare(b);
    });
    for (const o of extras) {
      const p = plugins.find((pl) => pl.id === o);
      out.push({ origin: o, label: p?.label || o });
    }
    return out;
  }, [cards, plugins]);

  // origins claimed by ANY custom dashboard — excluded from the stock board below
  const claimedOrigins = useMemo(
    () => new Set(dashboards.flatMap((d) => d.origins ?? [])),
    [dashboards],
  );
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    // filter only — ordering is the Board's job (per-lane, respecting the sort pref)
    const list = cards.filter((c) => {
      // a grouped card lives UNDER its representative — hidden from the board, shown in
      // the representative's detail (several jobs for one task manage as one)
      if (c.cached?.group) return false;
      // source filter: hide a card whose origin the user toggled OFF (the persisted HIDDEN
      // set). Uniform for built-in AND plugin/custom sources — anything not explicitly
      // hidden shows, so a new source is visible by default. Pinned jobs show regardless.
      if (!c.local.pinned && hiddenSources.has(c.origin)) return false;
      // a manual note tagged with a custom board id lives on THAT board, not here
      if (c.origin === "manual" && (c.cached?.manual?.board || "")) return false;
      // same exclusivity for plugin origins a dashboard has claimed (dashboards.json
      // "origins"): those cards live on THAT dashboard only — without this, every
      // visualagent-style source double-posts to the stock board too
      if (claimedOrigins.has(c.origin)) return false;
      if (!q) return true;
      const hay = `${c.external_id} ${c.title} ${c.agent_state || ""} ${c.links
        .map((l) => l.ref)
        .join(" ")}`.toLowerCase();
      return hay.includes(q);
    });
    return list;
  }, [cards, query, hiddenSources, claimedOrigins]);

  // members of each representative card (cached.group → that card's id), for the board
  // "⧉ N" badge and the detail's group panel
  const groupMembers = useMemo(() => {
    const m = new Map<string, Card[]>();
    for (const c of cards) {
      const g = c.cached?.group as string | undefined;
      if (g) (m.get(g) ?? m.set(g, []).get(g)!).push(c);
    }
    return m;
  }, [cards]);

  const selected =
    cards.find((c) => c.id === selectedId) ||
    (extraCard && extraCard.id === selectedId ? extraCard : null);
  // the card surface, resolved through the kernel seam: the active provider is
  // the core CardDetail drawer (kernel/bootstrap.ts), same component reference
  // and same props as the old direct import — mechanical indirection only.
  const CardSurface = cardSurface();
  // per-lane counts over the registry lane set (custom stages included); done is
  // rendered nowhere in the header, so it just goes unread here.
  // one pass, no pre-seeding and no membership guard: the three fixed badges default
  // with `?? 0`, and the custom-stage row only renders keys its own `> 0` filter already
  // matched. (Not a perf fix — measured ~0.05ms either way at 2884 cards.)
  //
  // A Map, not a plain object, because `c.lane` is server data: keying an object by it
  // puts `__proto__`/`constructor` on the write path (a silent no-op in strict mode
  // here, but the kind of thing you only notice once). Board.tsx buckets by lane the
  // same way, so the two passes now read alike.
  const counts = useMemo(() => {
    const out = new Map<string, number>();
    for (const c of visible) out.set(c.lane, (out.get(c.lane) ?? 0) + 1);
    return out;
  }, [visible]);
  // nav = Board, then each custom local dashboard, then the fixed tabs
  // nav order: board 0, dashboards 10+, then built-ins 30/40/50 (terminals/monitor);
  // plugin tabs slot in by their manifest `orders.tab` — this slot's OWN effective
  // order (e.g. orchestrator 20 → back near the front), independent of the plugin's
  // menu-bar or card-widget order, and user-reorderable from the manager panel's
  // "版面排序" → 側邊分頁 section.
  const nav = [
    { ...NAV[0], order: 0 },
    ...dashboards.map((d, i) => ({ id: d.id, icon: d.icon, label: d.label, order: 10 + i })),
    ...NAV.slice(1).map((n, i) => ({ ...n, order: 30 + i * 10 })),
    ...plugins.filter((p) => p.tab).map((p) => ({ id: p.id, icon: p.icon, label: p.label, order: p.orders?.tab ?? p.order })),
  ].sort((a, b) => a.order - b.order);
  const onCustomBoard = dashboards.some((d) => d.id === view);
  // hoisted out of the JSX: an inline .filter() mints a new array on every App render,
  // which would defeat Board's byLane memo (and through it CardItem's) on this path
  const dashboardCards = useMemo(() => {
    const q = query.trim().toLowerCase();
    // a dashboard entry may name plugin origins (dashboards.json "origins") whose
    // cards ALL belong to it — the seam that lets a plugin source (visualagent, …)
    // feed a custom board; manual cards keep their per-card board tag.
    const dashOrigins = new Set(dashboards.find((d) => d.id === view)?.origins ?? []);
    return cards.filter((c) => {
      const onBoard =
        c.origin === "manual"
          ? !c.cached?.group && (c.cached?.manual?.board ?? "") === view
          : dashOrigins.has(c.origin);
      if (!onBoard) return false;
      // the FilterPopover renders on every boardish view — a source toggle that did
      // nothing here would be a dead control (and the way back is the same popover)
      if (!c.local.pinned && hiddenSources.has(c.origin)) return false;
      if (!q) return true;
      // the same haystack the main board searches — the header renders ONE search box
      // for every boardish view, and on a custom board it silently did nothing
      const hay = `${c.external_id} ${c.title} ${c.agent_state || ""} ${c.links
        .map((l) => l.ref)
        .join(" ")}`.toLowerCase();
      return hay.includes(q);
    });
  }, [cards, view, query, hiddenSources, dashboards]);
  // dashboard-type views (main board + custom boards): the only ones search/filter act
  // on — other tabs (terminals/plugins) don't render them, and on mobile the header
  // collapses to just the menu-bar strip.
  const boardish = view === "board" || onCustomBoard;

  if (authState === "loading") {
    return <div className="h-full bg-zinc-950" />;
  }
  if (me && (authState === "anon" || (setupMode && !me.authenticated))) {
    return <Login me={me} onLoggedIn={onLoggedIn} />;
  }

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* relative sm:z-[55]: header popovers (updater, user menu, filter, any plugin
          menu widget) drop DOWN past the header, so they need to outrank the card
          drawer's z-50 or they render behind it — only their top edge showed. Safe on
          desktop because the drawer starts BELOW the header (CardDetail sets top=headerH),
          so raising the header covers nothing. Mobile keeps z-auto: there the drawer is a
          full-screen sheet and must cover the header. 55 stays under the fullscreen
          terminal (60) and the modals (70). */}
      <header className="relative sm:z-[55] flex flex-wrap items-center gap-2 gap-y-1.5 px-3 sm:px-4 py-2 border-b border-zinc-800 bg-zinc-900/60">
        <h1
          className={`text-base font-bold tracking-tight mr-1 items-center gap-1.5 ${
            boardish ? "flex" : "hidden sm:flex"
          }`}
        >
          <svg width="18" height="18" viewBox="0 0 32 32" className="inline-block">
            <rect width="32" height="32" rx="7" fill="#0f0f12" />
            <rect x="6.5" y="10" width="4.5" height="16" rx="2" fill="#64748b" />
            <rect x="13.75" y="6" width="4.5" height="20" rx="2" fill="#fbbf24" />
            <rect x="21" y="13" width="4.5" height="13" rx="2" fill="#38bdf8" />
          </svg>
          Conductor
        </h1>
        {boardish && (
          <>
            <div className="relative flex-1 min-w-[7rem] sm:flex-none sm:w-56">
              <input
                ref={searchRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="search…  ( / )"
                className="w-full text-sm px-2 py-1 pr-6 rounded bg-zinc-800 border border-zinc-700 text-zinc-100"
              />
              {query && (
                <button
                  onClick={() => {
                    setQuery("");
                    searchRef.current?.focus();
                  }}
                  className="absolute right-1 top-1/2 -translate-y-1/2 px-0.5 text-zinc-500 hover:text-zinc-200 leading-none"
                  title="clear search"
                >
                  ×
                </button>
              )}
            </div>
            <FilterPopover
              sources={sources}
              hidden={hiddenSources}
              setHidden={setHidden}
              sortBy={sortBy}
              setSortBy={setSortBy}
              showHidden={showHidden}
              setShowHidden={setShowHidden}
            />
          </>
        )}
        <div className="sm:ml-auto w-full sm:w-auto flex items-center gap-3 min-w-0 text-body-s">
          {/* mobile: this info strip scrolls left/right — no dropdowns inside, so overflow is safe */}
          <div className="flex-1 sm:flex-none min-w-0 flex items-center gap-3 overflow-x-auto sm:overflow-visible">
            <PluginMenuBar
              plugins={plugins}
              onOpenView={setView}
              onOpenCard={setSelectedId}
              counts={counts}
              lanes={lanes}
            />
            <UsageBadge />
          </div>
          {/* on mobile, non-board views keep just the menu-bar strip — these controls hide */}
          <div className={`items-center gap-3 shrink-0 ${boardish ? "flex" : "hidden sm:flex"}`}>
            <button
              onClick={() => setShowCreate(true)}
              className="shrink-0 px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700"
              title="new manual card"
            >
              + New
            </button>
            <button
              onClick={manualRefresh}
              disabled={refreshing}
              aria-busy={refreshing}
              className="shrink-0 px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 disabled:hover:bg-zinc-800"
              title={refreshing ? "refreshing…" : "refresh the board"}
            >
              <span className={`inline-block ${refreshing ? "animate-spin" : ""}`}>↻</span>
            </button>
            {/* AvatarMenu renders even when signed out / auth is off (placeholder gear
                avatar) — its dropdown carries theme (a display preference) and the
                connection-health detail, both of which must stay reachable without a
                session; email + sign-out only show up when a user is present. */}
            <AvatarMenu user={me?.user ?? null} onSignedOut={refreshAuth} connected={connected} stale={stale} />
          </div>
        </div>
      </header>
      {/* --ws-inset: an app-wide right inset PLUGINS may publish by setting the
          CSS var on <html> (e.g. the cardwindows workspace region cedes its
          width through it). Defaults to 0px — with no publisher this calc is
          exactly the old var(--dw) margin. CSS-var seam on purpose: reactive,
          zero-provider default, no render coupling. */}
      <div
        className="flex-1 min-h-0 flex sm:[margin-right:calc(var(--dw)_+_var(--ws-inset,0px))] transition-[margin-right] duration-150"
        style={{ ["--dw" as string]: `${selected ? drawerWidth : 0}px` }}
      >
        {/* desktop: left rail. mobile: bottom tab bar (thumb reach, §3F.1) */}
        <nav className="hidden sm:flex w-16 shrink-0 border-r border-zinc-800 bg-zinc-900/40 flex-col items-center py-2 gap-1">
          {nav.map((n) => (
            <button
              key={n.id}
              onClick={() => setView(n.id)}
              className={`w-[52px] flex flex-col items-center gap-0.5 py-2 rounded transition ${
                view === n.id ? "bg-sky-600/30 text-sky-200" : "text-zinc-400 hover:bg-zinc-800"
              }`}
              title={n.label}
            >
              <span className="text-lg leading-none">{n.icon}</span>
              <span className="text-[9px]">{n.label}</span>
            </button>
          ))}
        </nav>
        <main className="flex-1 min-h-0 min-w-0 overflow-hidden pb-[calc(3.5rem_+_env(safe-area-inset-bottom))] sm:pb-0">
          {view === "terminals" ? (
            <TerminalsPanel onOpenCard={(id) => setSelectedId(id)} />
          ) : plugins.some((p) => p.id === view && p.tab) ? (
            <PluginPanel plugin={plugins.find((p) => p.id === view)!} cards={cards} onOpenCard={setSelectedId} />
          ) : onCustomBoard ? (
            <Board
              cards={dashboardCards}
              lanes={lanes}
              members={groupMembers}
              sortBy={sortBy}
              onSelect={setSelectedId}
            />
          ) : isLoading && !cards.length ? (
            <div className="p-3 grid gap-2 sm:grid-cols-3 max-w-4xl">
              {Array.from({ length: 6 }).map((_, i) => (
                <CardSkeleton key={i} />
              ))}
            </div>
          ) : (
            <Board cards={visible} lanes={lanes} members={groupMembers} sortBy={sortBy} onSelect={setSelectedId} />
          )}
        </main>
      </div>
      {/* mobile bottom tab bar */}
      <nav className="sm:hidden fixed bottom-0 inset-x-0 z-40 flex border-t border-zinc-800 bg-surface-raised/95 backdrop-blur pb-[env(safe-area-inset-bottom)]">
        {nav.map((n) => (
          <button
            key={n.id}
            onClick={() => setView(n.id)}
            className={`flex-1 flex flex-col items-center gap-0.5 py-2 ${
              view === n.id ? "text-sky-300" : "text-zinc-500"
            }`}
          >
            <span className="text-lg leading-none">{n.icon}</span>
            <span className="text-[10px]">{n.label}</span>
          </button>
        ))}
      </nav>
      {selected && (
        <CardSurface
          card={selected}
          allCards={cards}
          width={drawerWidth}
          onResize={setDrawerWidth}
          onClose={() => setSelectedId(null)}
          onOpenCard={setSelectedId}
          onChanged={reload}
        />
      )}
      {/* always-mounted overlay surfaces ("surface.overlay", a kernel COLLECTION:
          every registered provider renders; zero providers — the core default —
          renders nothing). Providers register at module-eval time (plugin
          packages run before the first render), so resolving per render is
          stable and the index key is safe. Overlays own the z-[65] band: above
          the header (55) and the fullscreen terminal (60), under the modals (70). */}
      {surfaceOverlays().map((Overlay, i) => (
        <Overlay key={i} cards={cards} onOpenCard={setSelectedId} onChanged={reload} boardish={boardish} />
      ))}
      {showCreate && (
        <CreateCardModal
          onClose={() => setShowCreate(false)}
          onCreate={(body) =>
            createManualMut.mutateAsync({ ...body, board: onCustomBoard ? view : "" }).then(() => undefined)
          }
        />
      )}
    </div>
  );
}
