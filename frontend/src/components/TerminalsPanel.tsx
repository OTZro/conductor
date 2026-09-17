import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { adoptSession, getCard, listCards, listHosts, listTmux, newTmux, openTmux, stopTerminal } from "../api";
import { usePersisted } from "../persist";
import type { Card, HostInfo, TmuxSession } from "../types";
import { HostChip } from "./HostChip";
import { TerminalView } from "./TerminalView";

const KIND_DOT: Record<string, string> = {
  conductor: "text-emerald-400",
  other: "text-zinc-500",
};

type Active = { session: TmuxSession; url: string; id: string };

// job name (ticket + title) is the title; fall back to the raw tmux name
function jobLabel(s: TmuxSession): string {
  if (s.external_id) return `${s.external_id}${s.title ? ` · ${s.title}` : ""}`;
  return s.name;
}

// a card-less session running claude (has live pane status, not yet bound to a card):
// the one thing you can "adopt" onto a card.
const isUnboundClaude = (s: TmuxSession) => !!s.status && !s.card_id;

export function TerminalsPanel({
  onOpenCard,
}: {
  onClose?: () => void;
  onOpenCard: (cardId: string) => void;
}) {
  const [sessions, setSessions] = useState<TmuxSession[]>([]);
  const [hosts, setHosts] = useState<HostInfo[]>([]);
  const [active, setActive] = useState<Active | null>(null);
  const [busy, setBusy] = useState(false);
  const [fontSize, setFontSize] = useState(14);
  const [sidebarOpen, setSidebarOpen] = usePersisted("conductor.termSidebar", true);
  const [sort, setSort] = usePersisted<"recent" | "name" | "manual">(
    "conductor.termSort",
    "recent",
  );
  const [pins, setPins] = usePersisted<string[]>("conductor.termPins", []); // sk() keys, pinned to top
  const [order, setOrder] = usePersisted<string[]>("conductor.termOrder", []); // manual drag order
  const dragKey = useRef<string | null>(null);

  const reload = useCallback(() => {
    listTmux(true).then(setSessions).catch(() => {}); // ?status=1 → live pane state
    listHosts().then(setHosts).catch(() => {});
  }, []);

  useEffect(() => {
    reload();
    const t = setInterval(reload, 8000);
    return () => clearInterval(t);
  }, [reload]);

  // entering the tab lands on base's first tmux (roam's first as fallback) instead
  // of an empty pane. Once only — closing it on purpose must not re-open it.
  const autoOpened = useRef(false);
  useEffect(() => {
    if (autoOpened.current || active || busy || !sessions.length) return;
    // mobile: land on the session LIST (master-detail), don't jump straight into a pane
    if (!window.matchMedia("(min-width: 640px)").matches) return;
    const first = sessions.find((s) => !s.host) || sessions[0];
    if (!first) return;
    autoOpened.current = true;
    open(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions]);

  const localName = hosts.find((h) => h.local)?.name;

  // the ACTIVE session's owning card, fetched on demand — feeds TerminalView's plugin
  // ⋯-menu slot (fork etc.). The `cards` state below is adopt-picker-only and lazy,
  // so it can't serve this.
  const [activeCard, setActiveCard] = useState<Card | null>(null);
  useEffect(() => {
    const cid = active?.session.card_id;
    if (!cid) {
      setActiveCard(null);
      return;
    }
    let alive = true;
    getCard(cid)
      .then((c) => alive && setActiveCard(c))
      .catch(() => alive && setActiveCard(null));
    return () => {
      alive = false;
    };
  }, [active?.session.card_id]);

  // one group per machine — local first, then remotes; a configured host shows its
  // group even with zero sessions (or offline) so its state is visible at a glance
  const groups = useMemo(() => {
    const byKey = new Map<string, TmuxSession[]>();
    for (const s of sessions) {
      const k = s.host || "";
      if (!byKey.has(k)) byKey.set(k, []);
      byKey.get(k)!.push(s);
    }
    const names = new Set<string>(byKey.keys());
    for (const h of hosts) names.add(h.local ? "" : h.name);
    return [...names]
      .sort((a, b) => (a === "" ? -1 : b === "" ? 1 : a.localeCompare(b)))
      .map((k) => ({
        key: k || "local",
        host: k || null,
        list: byKey.get(k) ?? [],
        online: k === "" ? true : (hosts.find((h) => h.name === k)?.online ?? true),
      }));
  }, [sessions, hosts]);

  // stable per-session key across reloads (host + socket + tmux name). socket-less
  // (default-socket) sessions keep their original key → existing pins/order survive.
  const sk = (s: TmuxSession) => `${s.host || ""}:${s.socket ? s.socket + "/" : ""}${s.name}`;
  const pinned = useMemo(() => new Set(pins), [pins]);
  const togglePin = (s: TmuxSession) => {
    const k = sk(s);
    setPins(pins.includes(k) ? pins.filter((x) => x !== k) : [...pins, k]);
  };

  // pinned first, then the chosen sort (name / most-recent-activity / manual drag order)
  const arrange = (list: TmuxSession[]) => {
    const idx = (k: string) => {
      const i = order.indexOf(k);
      return i < 0 ? 1e9 : i;
    };
    const cmp = (a: TmuxSession, b: TmuxSession) =>
      sort === "name"
        ? (a.external_id || a.name).localeCompare(b.external_id || b.name)
        : sort === "recent"
          ? (b.activity || 0) - (a.activity || 0)
          : idx(sk(a)) - idx(sk(b));
    const p = list.filter((s) => pinned.has(sk(s))).sort(cmp);
    const r = list.filter((s) => !pinned.has(sk(s))).sort(cmp);
    return [...p, ...r];
  };

  // drop `fromKey` before `toKey` — seed a complete order from the current display so
  // the first drag works, and switch the sort mode to manual (drag implies manual).
  const reorder = (fromKey: string, toKey: string) => {
    if (!fromKey || fromKey === toKey) return;
    const shown = groups.flatMap((g) => arrange(g.list)).map(sk);
    const base = [...order.filter((k) => shown.includes(k))];
    for (const k of shown) if (!base.includes(k)) base.push(k);
    base.splice(base.indexOf(fromKey), 1);
    base.splice(base.indexOf(toKey), 0, fromKey);
    setOrder(base);
    if (sort !== "manual") setSort("manual");
  };

  const open = async (s: TmuxSession) => {
    setBusy(true);
    try {
      if (active) await stopTerminal(active.id).catch(() => {});
      const r = await openTmux(s.name, undefined, s.host, s.socket);
      setActive({ session: s, url: r.url, id: r.id });
    } finally {
      setBusy(false);
    }
  };

  // spin up a fresh non-conductor shell on this host and drop into it
  const newShell = async (host: string | null) => {
    setBusy(true);
    try {
      if (active) await stopTerminal(active.id).catch(() => {});
      const r = await newTmux(host);
      setActive({
        session: { name: r.tmux_session, kind: "other", attached: false, host: r.host ?? null },
        url: r.url,
        id: r.id,
      });
      reload();
    } finally {
      setBusy(false);
    }
  };

  // adopt: bind a hand-run claude session onto a card so it shows under it + drives its
  // lane. Picker opens over the sidebar; loads current cards for the "existing card" case.
  const [adopting, setAdopting] = useState<TmuxSession | null>(null);
  const [cards, setCards] = useState<Card[]>([]);
  const [cardFilter, setCardFilter] = useState("");
  const openAdopt = (s: TmuxSession) => {
    setAdopting(s);
    setCardFilter("");
    listCards()
      .then((cs) => setCards(cs.filter((c) => c.lane !== "done")))
      .catch(() => setCards([]));
  };
  const doAdopt = async (s: TmuxSession, cardId?: string) => {
    setBusy(true);
    try {
      await adoptSession(s.name, s.host, cardId);
      setAdopting(null);
      reload(); // the row re-labels to the bound card on the next list
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="h-full flex flex-col sm:flex-row bg-zinc-950">
      <aside
        className={`${active ? "hidden" : "flex"} ${sidebarOpen ? "sm:flex" : "sm:hidden"} flex-col w-full sm:w-80 flex-1 sm:flex-none sm:shrink-0 min-h-0 border-b sm:border-b-0 sm:border-r border-zinc-800`}
      >
        <div className="flex items-center gap-2 px-3 py-2 border-b border-zinc-800">
          <h2 className="text-sm font-semibold">Terminals</h2>
          <span className="text-xs text-zinc-500">{sessions.length}</span>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as typeof sort)}
            className="ml-auto text-[11px] px-1 py-0.5 rounded bg-zinc-800 border border-zinc-700 text-zinc-300"
            title="sort sessions"
          >
            <option value="recent">recent</option>
            <option value="name">name</option>
            <option value="manual">manual (drag)</option>
          </select>
          <button
            onClick={reload}
            className="text-xs px-1.5 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700"
            title="refresh"
          >
            ↻
          </button>
          <button
            onClick={() => setSidebarOpen(false)}
            className="hidden sm:block text-xs px-1.5 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700"
            title="collapse sidebar — give the width to the terminal"
          >
            «
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-2">
          {groups.map((g) => (
            <div key={g.key} className="space-y-1">
              <div className="flex items-center gap-1.5 px-1 pt-1">
                <HostChip host={g.host} localName={localName} />
                <span className="text-[10px] text-zinc-500">{g.list.length}</span>
                {!g.online && <span className="text-[10px] text-red-400/80">offline</span>}
                {g.online && (
                  <button
                    disabled={busy}
                    onClick={() => newShell(g.host)}
                    className="ml-auto text-[11px] leading-none px-1.5 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-300 disabled:opacity-50"
                    title={`new shell on ${g.host || localName || "base"} — a plain tmux session, not tied to any card`}
                  >
                    ＋
                  </button>
                )}
              </div>
              {arrange(g.list).map((s) => (
                <div
                  key={`${g.key}:${s.socket || ""}:${s.name}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => !busy && open(s)}
                  draggable={sort === "manual"}
                  onDragStart={() => (dragKey.current = sk(s))}
                  onDragOver={(e) => sort === "manual" && e.preventDefault()}
                  onDrop={(e) => {
                    e.preventDefault();
                    if (dragKey.current) reorder(dragKey.current, sk(s));
                    dragKey.current = null;
                  }}
                  className={`group relative w-full text-left px-2 py-1.5 rounded border ${
                    busy ? "opacity-50" : "cursor-pointer"
                  } ${sort === "manual" ? "cursor-grab active:cursor-grabbing" : ""} ${
                    active?.session.name === s.name &&
                    (active?.session.host ?? null) === (s.host ?? null) &&
                    // "absent" arrives as null from the backend list but undefined from
                    // newShell's literal — normalise or the fresh shell never highlights
                    (active?.session.socket ?? null) === (s.socket ?? null)
                      ? "bg-zinc-800 border-zinc-600"
                      : "bg-zinc-900 border-zinc-800 hover:bg-zinc-800"
                  }`}
                >
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      togglePin(s);
                    }}
                    className={`absolute right-1 top-1 text-[11px] leading-none px-1 rounded ${
                      pinned.has(sk(s))
                        ? "text-amber-300"
                        : "text-zinc-600 opacity-0 group-hover:opacity-100 hover:text-zinc-300"
                    }`}
                    title={pinned.has(sk(s)) ? "unpin" : "pin to top"}
                  >
                    📌
                  </button>
                  {isUnboundClaude(s) && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        openAdopt(s);
                      }}
                      className="absolute right-6 top-1 text-[11px] leading-none px-1 rounded text-zinc-600 opacity-0 group-hover:opacity-100 hover:text-emerald-300"
                      title="bind this claude session to a card"
                    >
                      🔗
                    </button>
                  )}
                  <div className="text-xs truncate flex items-center gap-1 pr-4">
                    {sort === "manual" && <span className="text-zinc-600 shrink-0">⠿</span>}
                    {s.status ? (
                      <span
                        className={
                          s.status.state === "running"
                            ? "text-emerald-400 animate-pulse"
                            : "text-amber-400/80"
                        }
                        title={s.status.state === "running" ? "AI working" : "idle · waiting for you"}
                      >
                        ●
                      </span>
                    ) : (
                      <span className={KIND_DOT[s.kind] || "text-zinc-500"}>●</span>
                    )}{" "}
                    {s.external_id ? (
                      <span className="font-medium text-zinc-100 truncate">{jobLabel(s)}</span>
                    ) : (
                      <span className="font-mono text-zinc-300 truncate">{s.name}</span>
                    )}
                    {s.socket && (
                      <span
                        className={`text-[9px] px-1 rounded shrink-0 ${
                          s.readonly
                            ? "bg-indigo-500/20 text-indigo-300"
                            : "bg-amber-500/20 text-amber-300"
                        }`}
                        title={
                          s.readonly
                            ? `tmux -L ${s.socket} · read-only view (another system's managed pane)`
                            : `tmux -L ${s.socket} · WRITABLE — typing here competes with the agent's own automated input`
                        }
                      >
                        {s.socket} {s.readonly ? "·ro" : "·rw"}
                      </span>
                    )}
                    {s.attached && <span className="text-[9px] text-zinc-500 shrink-0"> ·attached</span>}
                  </div>
                  {/* live status line: what Claude is doing right now */}
                  {s.status && (
                    <div className="flex items-center gap-1.5 mt-0.5 text-[10px] flex-wrap">
                      <span
                        className={
                          s.status.state === "running" ? "text-emerald-400" : "text-zinc-500"
                        }
                      >
                        {s.status.state === "running" ? "▶ running" : "idle"}
                      </span>
                      {s.status.bg && (
                        <span className="text-sky-300" title="background task in flight">
                          ⚙ {s.status.bg}
                        </span>
                      )}
                      {s.status.model && <span className="text-zinc-500">{s.status.model}</span>}
                      {s.status.ctx_pct != null && (
                        <span
                          className={s.status.ctx_pct >= 80 ? "text-amber-400" : "text-zinc-500"}
                          title="context window used"
                        >
                          ctx {s.status.ctx_pct}%
                        </span>
                      )}
                    </div>
                  )}
                  {(s.status?.summary || s.status?.task) && (
                    <div
                      className="text-[11px] text-zinc-400 truncate mt-0.5"
                      title={s.status.summary || s.status.task || ""}
                    >
                      {s.status.summary || `“${s.status.task}”`}
                    </div>
                  )}
                  {s.external_id && (
                    <div className="text-[10px] text-zinc-600 font-mono truncate mt-0.5">
                      {s.name}
                    </div>
                  )}
                </div>
              ))}
              {!g.list.length && (
                <div className="text-[11px] text-zinc-600 px-2 py-1">
                  {g.online ? "no sessions" : "unreachable — sessions hidden until it's back"}
                </div>
              )}
            </div>
          ))}
        </div>
      </aside>
      {/* collapsed rail — desktop only; mobile uses master-detail (list ⇄ pane) */}
      {!sidebarOpen && (
        <button
          onClick={() => setSidebarOpen(true)}
          title="show sessions"
          className="hidden sm:flex shrink-0 flex-col items-center justify-center gap-1 py-3 border-r border-zinc-800 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40"
        >
          <span className="text-sm">»</span>
          <span className="text-[11px] [writing-mode:vertical-rl] tracking-wide">
            sessions {sessions.length}
          </span>
        </button>
      )}
      <main className={`${active ? "flex" : "hidden sm:flex"} flex-1 flex-col bg-black min-w-0 p-1`}>
        {active ? (
          <TerminalView
            url={active.url}
            sid={active.id}
            host={active.session.host}
            card={activeCard}
            fontSize={fontSize}
            fill
            onZoom={async (s) => {
              setFontSize(s);
              await stopTerminal(active.id).catch(() => {});
              const r = await openTmux(active.session.name, s, active.session.host, active.session.socket);
              setActive({ session: active.session, url: r.url, id: r.id });
            }}
            onDead={async () => {
              // dead ttyd (backend restart) — drop the stale row + reopen a fresh viewer
              await stopTerminal(active.id).catch(() => {});
              const r = await openTmux(active.session.name, fontSize, active.session.host, active.session.socket);
              setActive({ session: active.session, url: r.url, id: r.id });
            }}
            onClose={async () => {
              await stopTerminal(active.id).catch(() => {});
              setActive(null);
            }}
            headerExtra={
              <>
                <button
                  onClick={async () => {
                    await stopTerminal(active.id).catch(() => {});
                    setActive(null);
                  }}
                  className="sm:hidden shrink-0 px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-200"
                  title="back to session list"
                >
                  ‹ list
                </button>
                <span className="text-zinc-300 truncate max-w-[40%]">
                  {jobLabel(active.session)}
                </span>
                <HostChip host={active.session.host} localName={localName} />
                {active.session.card_id ? (
                  <button
                    onClick={() => onOpenCard(active.session.card_id!)}
                    className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-200"
                  >
                    open task →
                  </button>
                ) : (
                  isUnboundClaude(active.session) && (
                    <button
                      onClick={() => openAdopt(active.session)}
                      className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-emerald-300"
                      title="bind this claude session to a card"
                    >
                      🔗 bind card
                    </button>
                  )
                )}
              </>
            }
          />
        ) : (
          <div className="flex-1 flex items-center justify-center text-zinc-600 text-sm">
            select a session to attach
          </div>
        )}
      </main>

      {adopting && (
        <div
          className="fixed inset-0 z-[70] bg-black/50 flex items-center justify-center"
          onClick={() => setAdopting(null)}
        >
          <div
            className="w-[420px] max-w-[92vw] max-h-[80vh] flex flex-col bg-zinc-900 border border-zinc-700 rounded-xl p-4 gap-3 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="text-sm">
              <div className="font-semibold text-zinc-100">Bind session to a card</div>
              <div className="text-[11px] text-zinc-500 font-mono truncate mt-0.5">
                {adopting.host ? `${adopting.name} · ${adopting.host}` : adopting.name}
              </div>
              {adopting.status?.summary && (
                <div className="text-[11px] text-zinc-400 truncate mt-0.5">
                  {adopting.status.summary}
                </div>
              )}
            </div>
            <button
              disabled={busy}
              onClick={() => doAdopt(adopting)}
              className="text-sm px-3 py-1.5 rounded bg-emerald-500/80 hover:bg-emerald-500 text-zinc-900 font-medium disabled:opacity-40 text-left"
            >
              ＋ New card from this session
            </button>
            <div className="text-[11px] text-zinc-500">…or bind to an existing card:</div>
            <input
              autoFocus
              value={cardFilter}
              onChange={(e) => setCardFilter(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && setAdopting(null)}
              placeholder="filter cards…"
              className="w-full text-sm px-2.5 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-zinc-100"
            />
            <div className="flex-1 overflow-y-auto -mx-1 px-1 space-y-1 min-h-0">
              {cards
                .filter((c) => {
                  const q = cardFilter.trim().toLowerCase();
                  return (
                    !q ||
                    c.title.toLowerCase().includes(q) ||
                    c.external_id.toLowerCase().includes(q)
                  );
                })
                .slice(0, 60)
                .map((c) => (
                  <button
                    key={c.id}
                    disabled={busy}
                    onClick={() => doAdopt(adopting, c.id)}
                    className="block w-full text-left px-2 py-1.5 rounded bg-zinc-800/60 hover:bg-zinc-800 border border-zinc-800 disabled:opacity-40"
                  >
                    <div className="text-xs text-zinc-200 truncate">
                      {c.title || c.external_id}
                    </div>
                    <div className="text-[10px] text-zinc-500 font-mono truncate">
                      {c.origin} · {c.external_id}
                    </div>
                  </button>
                ))}
              {!cards.length && (
                <div className="text-[11px] text-zinc-600 px-1 py-2">no open cards</div>
              )}
            </div>
            <button
              onClick={() => setAdopting(null)}
              className="text-sm px-3 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 self-end"
            >
              cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
