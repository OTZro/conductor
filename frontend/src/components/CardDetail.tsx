import { type MouseEvent as ReactMouseEvent, useEffect, useRef, useState } from "react";
import {
  addLink,
  getAwaiting,
  getBody,
  getDefaultCwd,
  getDirs,
  getLaunchProfiles,
  getSlackThread,
  groupCard,
  handoverPrep,
  handoverTransfer,
  killTerminal,
  listConversations,
  listHosts,
  listTmux,
  openTerminal,
  openTmux,
  patchState,
  prAction,
  refreshCardPrs,
  resumeCard,
  setDone,
  setHold,
  stopTerminal,
} from "../api";
import type { TConversation, TLaunchProfile, TSlackMsg } from "../api";
import type { AwaitingInput, Card, HostInfo, TmuxSession } from "../types";
import { splitReviewers } from "../cardMeta";
import { useToast } from "../ui/toast";
import { usePerCard } from "../persist";
import { HoverCard } from "../ui/HoverCard";
import { Btn } from "../ui/primitives";
import { FocusBlock } from "./FocusBlock";
import { HostChip } from "./HostChip";
import { LinksPanel } from "./LinksPanel";
import { Markdown } from "./Markdown";
import { terminalSurface } from "../kernel/seams";
import { PluginCardActions, PluginCardSlot, PluginCardWidgets } from "./PluginCardWidgets";

function mkLink(raw: string): { kind: string; ref: string; url: string; title?: string } {
  const s = raw.trim();
  if (/^[A-Z][A-Z0-9]+-\d+$/.test(s))
    return { kind: "jira", ref: s, url: `https://your-org.atlassian.net/browse/${s}`, title: s };
  const pr = s.match(/github\.com\/([\w.-]+\/[\w.-]+)\/pull\/(\d+)/);
  if (pr) return { kind: "pr", ref: `${pr[1]}#${pr[2]}`, url: s, title: `${pr[1]}#${pr[2]}` };
  const short = s.match(/^([\w.-]+\/[\w.-]+)#(\d+)$/);
  if (short)
    return { kind: "pr", ref: s, url: `https://github.com/${short[1]}/pull/${short[2]}`, title: s };
  if (/slack\.com\/archives\//.test(s)) return { kind: "slack", ref: s, url: s, title: "slack" };
  return { kind: "url", ref: s, url: s.startsWith("http") ? s : `https://${s}` };
}

// snooze durations offered in the Snooze button's hover panel; a plain click on the
// button itself uses the 4h default (hover to pick, click for default — on touch the
// tap peeks the panel instead, per HoverCard's peek-first pattern)
const SNOOZE_CHOICES = [
  { hours: 1, label: "1 hour" },
  { hours: 4, label: "4 hours" },
  { hours: 8, label: "8 hours" },
  { hours: 24, label: "1 day" },
  { hours: 72, label: "3 days" },
  { hours: 168, label: "1 week" },
];

// per-reviewer state → chip look (matches the board card's review verdict palette)
const PR_REVIEW: Record<string, { icon: string; cls: string }> = {
  approved: { icon: "✓", cls: "text-emerald-300 border-emerald-500/30 bg-emerald-500/10" },
  changes: { icon: "✗", cls: "text-red-300 border-red-500/30 bg-red-500/10" },
  commented: { icon: "💬", cls: "text-amber-200 border-amber-500/30 bg-amber-500/10" },
  dismissed: { icon: "⊘", cls: "text-zinc-400 border-zinc-600 bg-zinc-700/30" },
  pending: { icon: "⏳", cls: "text-zinc-400 border-zinc-600 bg-zinc-700/30" },
};

function ciCls(ci?: string): string {
  return ci === "failing"
    ? "text-red-300"
    : ci === "pending"
      ? "text-amber-300"
      : ci === "passing"
        ? "text-emerald-300"
        : "text-zinc-400";
}

export function CardDetail({
  card,
  allCards = [],
  width,
  onResize,
  onClose,
  onOpenCard,
  onChanged,
  compactHeader = false,
}: {
  card: Card;
  allCards?: Card[]; // every card — for the group panel (members + the add-member picker)
  width: number;
  onResize: (w: number) => void;
  onClose: () => void;
  onOpenCard?: (id: string) => void;
  onChanged: () => void;
  // hide the header block (origin/id/title/action row) — a compact HOST
  // (e.g. a low workspace tile) renders title + actions in its own chrome.
  // See CardSurfaceProps in kernel/seams.ts. The drawer never sets this.
  compactHeader?: boolean;
}) {
  // the terminal host, resolved through the kernel seam: the active provider is
  // the core TerminalView (kernel/bootstrap.ts) — same component, same props.
  const TerminalSurface = terminalSurface();
  // block focus (⤢, see FocusBlock.tsx): which content block fills the card.
  // Per-instance VIEW state, reset on card switch DURING RENDER (the same
  // pattern usePerCard documents — an effect would flash the old card's
  // focused block for a frame). Never persisted.
  const [focusBlock, setFocusBlock] = useState<string | null>(null);
  const [focusCard, setFocusCard] = useState(card.id);
  if (focusCard !== card.id) {
    setFocusCard(card.id);
    setFocusBlock(null);
  }
  const termFocused = focusBlock === "terminal";
  // focused-sessions accordion: with the terminal block focused, ALL of the
  // section's chrome (action buttons, new-session form, session list — and
  // TerminalView's own toolbar via hideHeader) folds into one slim row so the
  // pane gets essentially the whole card. Session-local, never persisted.
  const [termChromeOpen, setTermChromeOpen] = useState(false);
  const [awaiting, setAwaiting] = useState<AwaitingInput | null>(null);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const { toast, error } = useToast();
  const [termUrl, setTermUrl] = useState<string | null>(null);
  const [termId, setTermId] = useState<string | null>(null);
  const [termHost, setTermHost] = useState<string | null>(null); // machine of the OPEN ttyd
  const [termTmux, setTermTmux] = useState<string | null>(null); // tmux the open ttyd shows (drives the active tab)
  const [handingOff, setHandingOff] = useState(false); // a hand-off poll is in flight
  const handoverAbort = useRef<{ v: boolean } | null>(null); // cancel an in-flight hand-off
  const [fontSize, setFontSize] = useState(14);
  const [headerH, setHeaderH] = useState(0); // app header height → drawer starts below it (desktop)
  const [showRaw, setShowRaw] = useState(false);
  const [linkUrl, setLinkUrl] = useState("");
  const [cwd, setCwd] = useState("");
  const [body, setBody] = useState<{ kind: string; content: string } | null>(null);
  const [slackThread, setSlackThread] = useState<TSlackMsg[]>([]);
  const [sessions, setSessions] = useState<TmuxSession[]>([]); // live tmux for this card
  const [convs, setConvs] = useState<TConversation[]>([]); // past claude conversations (dead ones resumable)
  const [resumeMenu, setResumeMenu] = useState(false); // Resume ▾ session picker
  const [newOpen, setNewOpen] = useState(false); // New-session config (cwd + host) panel
  // per CARD, not per app: collapsing one ticket's description says nothing about the
  // next one's, and a shared key made every card follow whichever was closed last
  const [prDetailOpen, setPrDetailOpen] = usePerCard("prDetail", card.id, true); // PR reviewers/checks vs summary
  const [bodyOpen, setBodyOpen] = usePerCard("body", card.id, true); // description/thread collapse
  const [awaitingOpen, setAwaitingOpen] = usePerCard("awaiting", card.id, true); // workpad collapse
  const [termOpen, setTermOpen] = usePerCard("terminal", card.id, true); // tmux session list + pane
  const [hosts, setHosts] = useState<HostInfo[]>([]); // machines a session can run on
  const [selHost, setSelHost] = useState(""); // "" = local (base); else remote name
  const [profiles, setProfiles] = useState<TLaunchProfile[]>([]); // ~/.conductor/profiles.json
  const [profile, setProfile] = useState(""); // selected launch profile ("" = none)
  // working-dir existence + completions on the chosen host (valid=null → unchecked)
  const [dirHint, setDirHint] = useState<{ valid: boolean | null; dirs: string[] }>({
    valid: null,
    dirs: [],
  });
  const [cwdFocus, setCwdFocus] = useState(false); // show the ghost completion only while editing

  const localName = hosts.find((h) => h.local)?.name;
  const isHold = card.origin === "jira" && card.agent_state === "awaiting input";
  // the actual hold LABEL (not the ball-derived isHold, which a higher signal
  // like a running agent can mask) — drives the Hold/Release toggle button
  const isHeld = ((card.cached?.jira?.labels as string[] | undefined) || []).includes("conductor-hold");
  const isPR = card.origin === "github";
  const gh = card.cached?.github || {};
  const prReviews: { user: string; state: string }[] = gh.reviews || [];
  const { humans: prHumans, ai: prAi } = splitReviewers(prReviews);
  const prReqs: string[] = gh.review_requests || [];
  const prChecks: { name: string; state: string; url?: string }[] = gh.checks || [];
  const prHasDetail = prReviews.length > 0 || prReqs.length > 0 || prChecks.length > 0;
  const lastSid = card.local.claude_session_id;
  // resumable conversations come from `convs` (TerminalSession history), NOT the
  // local_state pointer — a session's tmux can end (claude exits) while its
  // transcript stays resumable, and local_state.claude_session_id can be empty.
  // Prefer the remembered one, else the newest; this is what the Resume button binds.
  const primaryConv = convs.find((c) => c.claude_session_id === lastSid) || convs[0] || null;
  const isSnoozed = !!card.local.snoozed_until && new Date(card.local.snoozed_until) > new Date();

  // measure the app header so the desktop drawer can start just below it instead of
  // overlaying the menu bar. Re-measured on resize (the header can wrap → taller).
  useEffect(() => {
    const measure = () =>
      setHeaderH(document.querySelector("header")?.getBoundingClientRect().height ?? 0);
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  // on-demand: pull fresh PR status when a jira/github detail opens (the background
  // poll is throttled ~10min per card), then reload so the section reflects it.
  useEffect(() => {
    if (card.origin !== "jira" && card.origin !== "github") return;
    let alive = true;
    refreshCardPrs(card.id)
      .then((r) => {
        if (alive && r.ok) onChanged();
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.id]);

  useEffect(() => {
    setAwaiting(null);
    setAnswer("");
    setMsg(null);
    setTermUrl(null);
    setTermId(null);
    setTermHost(null);
    setTermTmux(null);
    setSessions([]);
    setResumeMenu(false);
    setNewOpen(false);
    setShowRaw(false);
    // Pre-fill the last dir this card ran in; else leave blank and fill from the
    // BACKEND's configured default (CONDUCTOR_DEFAULT_WORKSPACE_ROOT) — never a
    // hardcoded path, which would mask the env var and be sent as an override.
    // Blank while the fetch is in flight resolves to the same default.
    setCwd(card.local.workdir || "");
    setBody(null);
    setSlackThread([]);
    setSelHost("");
    setProfile("");
    listHosts().then(setHosts).catch(() => setHosts([]));
    getLaunchProfiles().then(setProfiles).catch(() => setProfiles([]));
    getBody(card.id).then(setBody).catch(() => setBody(null));
    if (card.origin === "slack")
      getSlackThread(card.id).then(setSlackThread).catch(() => setSlackThread([]));
    if (isHold) getAwaiting(card.id).then(setAwaiting).catch(() => setAwaiting(null));
    // auto-load this job's session on open. Prefer a HOOKED resume of OUR Conductor
    // session (local.claude_session_id) so prompts you type flip it to AI Working;
    // otherwise attach any other live session read-only.
    let autoId: string | null = null;
    let cancelled = false;
    // fill the New-session dir from the backend's configured default when the card has
    // no remembered workdir (functional update: never clobber what the user typed).
    if (!card.local.workdir)
      getDefaultCwd(card.id)
        .then((r) => {
          if (!cancelled) setCwd((c) => c || r.cwd);
        })
        .catch(() => {});
    (async () => {
      // FAST PATH — before anything that awaits. A card that DECLARES a watch target
      // (cached.watch.session, e.g. an agent-team plugin naming a member's tmux) needs none of the
      // discovery below: the backend resolves and liveness-checks it, and 400s if it's
      // gone. Waiting on listTmux() first only delayed the terminal by a round-trip —
      // and that scan can't even see these sessions, which live on their own socket.
      if (card.cached?.watch?.session) {
        try {
          const t = await openTerminal(card.id, "attach", { font_size: fontSize });
          if (cancelled) {
            stopTerminal(t.id).catch(() => {});
            return;
          }
          autoId = t.id;
          setTermUrl(t.url || null);
          setTermId(t.id);
          setTermHost(t.host ?? null);
          setTermTmux(card.cached.watch.session);
          return;
        } catch {
          /* nothing live to watch right now → fall through to the normal scan */
        }
      }
      // Only SHOW what's already RUNNING — never auto-revive a killed session on open
      // (reviving a past conversation is the Resume button / conversation list's job).
      // So decide off the live tmux list, not off lastSid alone.
      const sess = await listTmux().catch(() => []);
      if (cancelled) return;
      const mine = sess.filter((s) => s.card_id === card.id);
      const ownName = lastSid ? `conductor-${lastSid.slice(0, 8)}` : null;
      // Prefer a HOOKED, writable resume-attach of OUR live conductor session (typed
      // prompts flip it to AI Working); attach_only makes the backend attach-not-revive.
      if (ownName && mine.some((s) => s.name === ownName)) {
        try {
          const t = await openTerminal(card.id, "resume", { font_size: fontSize, attach_only: true });
          if (cancelled) {
            stopTerminal(t.id).catch(() => {});
            return;
          }
          autoId = t.id;
          setTermUrl(t.url || null);
          setTermId(t.id);
          setTermHost(t.host ?? null);
          setTermTmux(t.claude_session_id ? `conductor-${t.claude_session_id.slice(0, 8)}` : null);
          return;
        } catch {
          /* raced to death between the list and the attach → fall through */
        }
      }
      // otherwise attach any OTHER live session for this card (adopted) read
      // -only; if there's nothing else live, open NOTHING (no revive). Deliberately not
      // `?? mine[0]`: were our own session mid-death it'd be mine[0], and reselecting it
      // just opens a broken `tmux attach` pane. ownName===null (no lastSid) still yields
      // mine[0] here, since no session name equals null.
      const other = mine.find((s) => s.name !== ownName);
      if (!other || cancelled) return;
      try {
        const r = await openTmux(other.name, undefined, other.host);
        if (cancelled) {
          stopTerminal(r.id).catch(() => {});
          return;
        }
        autoId = r.id;
        setTermUrl(r.url);
        setTermId(r.id);
        setTermHost(other.host ?? null);
        setTermTmux(other.name);
      } catch {
        /* ignore */
      }
    })();
    listTmux()
      .then((all) => {
        if (!cancelled) setSessions(all.filter((s) => s.card_id === card.id));
      })
      .catch(() => {});
    listConversations(card.id)
      .then((c) => {
        if (!cancelled) setConvs(c);
      })
      .catch(() => setConvs([]));
    return () => {
      cancelled = true;
      if (autoId) stopTerminal(autoId).catch(() => {}); // don't leak the auto ttyd
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.id]);

  // Toast as WELL as the inline box. `msg` renders at the top of the drawer's scroll
  // container; the actions that set it are spread down the whole column — Approve and
  // Merge sit five sections below it. So pressing Approve put its result, success or
  // failure alike, somewhere off-screen, and the button looked like it did nothing.
  // That is what made a failing approve indistinguishable from a stale badge. The
  // toast is viewport-fixed, so the answer lands where the click did. Every one of
  // run()'s ~15 call sites gets this, not just the PR actions.
  const run = async (fn: () => Promise<unknown>, okMsg: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      setMsg(okMsg);
      if (okMsg) toast(okMsg, { tone: "ok" }); // the read-toggle passes "" on purpose
      onChanged();
    } catch (e: any) {
      const detail = `${e?.message || e}`;
      setMsg(`⚠ ${detail}`);
      error(detail);
    } finally {
      setBusy(false);
    }
  };

  const loadSessions = () => {
    listTmux()
      .then((all) => setSessions(all.filter((s) => s.card_id === card.id)))
      .catch(() => {});
    listConversations(card.id).then(setConvs).catch(() => {});
  };

  // a new session's tmux is created ASYNCHRONOUSLY by ttyd, so it isn't in listTmux the
  // instant openTerminal returns — reload a few times to catch it (the poll below then
  // keeps it fresh), so a just-created session lands in the list without a manual "sync".
  const refreshSessionsSoon = () => {
    loadSessions();
    setTimeout(loadSessions, 800);
    setTimeout(loadSessions, 1800);
  };

  // keep the live-session list fresh without a manual sync while the drawer is open —
  // sessions also start (auto-resume on open) or die outside this view.
  useEffect(() => {
    const t = setInterval(loadSessions, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.id]);

  // debounced: does the typed working dir exist on the selected run-on host, and what
  // sub-dirs complete it? Skip an offline remote (its ssh probe would just time out into
  // a misleading "not found").
  useEffect(() => {
    if (!newOpen || !cwd.trim()) {
      setDirHint({ valid: null, dirs: [] });
      return;
    }
    const h = hosts.find((x) => (selHost === "" ? x.local : x.name === selHost));
    if (h && !h.local && !h.online) {
      setDirHint({ valid: null, dirs: [] });
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      getDirs(cwd.trim(), selHost || undefined)
        .then((r) => live && setDirHint(r))
        .catch(() => live && setDirHint({ valid: null, dirs: [] }));
    }, 250);
    return () => {
      live = false;
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cwd, selHost, newOpen, hosts]);

  // inline "ghost" completion: the first suggestion that EXTENDS what's typed. Prefer a
  // same-case match (perfect preview); else fall back to a case-INSENSITIVE one so the
  // ghost still shows for e.g. "…/open" → "…/OpenCWB" (Tab then fixes the case). Accept
  // with Tab.
  const cwdGhost =
    cwd.trim() && cwdFocus
      ? (dirHint.dirs.find((d) => d.startsWith(cwd) && d.length > cwd.length) ??
        dirHint.dirs.find(
          (d) => d.toLowerCase().startsWith(cwd.toLowerCase()) && d.length > cwd.length,
        ) ??
        "")
      : "";

  // resume a specific past conversation — works for killed sessions too
  // (`claude --resume <sid>` revives the conversation in a fresh tmux on its host)
  const resumeConv = async (c: TConversation) => {
    setBusy(true);
    setMsg(null);
    try {
      const t = await openTerminal(card.id, "resume", {
        // "" = explicitly base — `?? undefined` would DROP the key and the backend
        // would fall back to the card's last-used host (how base conversations once
        // got resumed on roam)
        claude_session_id: c.claude_session_id,
        host: c.host ?? "",
        font_size: fontSize,
      });
      stopPrevView();
      setTermUrl(t.url || null);
      setTermId(t.id);
      setTermHost(t.host ?? null);
      setTermTmux(`conductor-${c.claude_session_id.slice(0, 8)}`);
      onChanged();
      loadSessions();
    } catch (e: any) {
      setMsg(`⚠ ${e?.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  // tab semantics: switching sessions closes the previous ttyd VIEW (its tmux/claude
  // keeps running) so views don't stack up.
  const stopPrevView = () => {
    if (termId) stopTerminal(termId).catch(() => {});
  };

  const openSession = async (s: TmuxSession) => {
    if (s.name === termTmux && (s.host ?? null) === (termHost ?? null)) return; // already showing
    setBusy(true);
    setMsg(null);
    try {
      const r = await openTmux(s.name, undefined, s.host);
      stopPrevView();
      setTermUrl(r.url);
      setTermId(r.id);
      setTermHost(s.host ?? null);
      setTermTmux(s.name);
    } catch (e: any) {
      setMsg(`⚠ ${e?.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  // How the auto/zoom/dead paths should REOPEN the pane. A card that declares a plugin
  // Watch target (cached.watch — e.g. an agent-team plugin naming a member's live tmux) re-attaches
  // its READ-ONLY watch; it never spawns/resumes a claude it doesn't have — so a dropped
  // watch (backend restart, websocket blip) self-heals in place instead of needing the
  // card closed + reopened. own cards keep resume-else-own.
  const reopenKind = (): "attach" | "resume" | "own" =>
    card.cached?.watch?.session ? "attach" : lastSid ? "resume" : "own";

  const doTerminal = async (
    kind: "attach" | "own" | "resume",
    sizeOverride?: number,
  ) => {
    setBusy(true);
    setMsg(null);
    try {
      const t = await openTerminal(card.id, kind, {
        // the dir field belongs to New session / Resume; attach runs in a directory
        // the BACKEND resolves, and sending this pre-filled value would only fight it
        cwd: kind === "attach" ? undefined : cwd.trim() || undefined,
        font_size: sizeOverride ?? fontSize,
        // only a NEW session picks its machine; resume follows the conversation's
        // host (remembered server-side) and attach is always local
        host: kind === "own" ? selHost || undefined : undefined,
        // a launch profile (own only) injects its env; host/cwd are already filled from it
        profile: kind === "own" ? profile || undefined : undefined,
      });
      stopPrevView();
      setTermUrl(t.url || null);
      setTermId(t.id);
      setTermHost(t.host ?? null);
      setTermTmux(
        // the backend names the tmux (it owns the rule, and for a live worker it also
        // owns WHICH session) — take its answer rather than rebuilding one here
        t.tmux_session
          ? t.tmux_session
          : t.claude_session_id
            ? `conductor-${t.claude_session_id.slice(0, 8)}`
            : null,
      );
      onChanged();
      refreshSessionsSoon(); // catch the just-created (async) tmux without a manual sync
    } catch (e: any) {
      setMsg(`⚠ ${e?.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  const closeTerminal = async () => {
    if (termId) {
      try {
        await stopTerminal(termId);
      } catch {
        /* ignore */
      }
    }
    setTermUrl(null);
    setTermId(null);
    setTermHost(null);
    setTermTmux(null);
  };

  const killTerm = async () => {
    if (termId) {
      try {
        await killTerminal(termId); // stops ttyd AND kills the tmux session (claude dies)
      } catch {
        /* ignore */
      }
    }
    setTermUrl(null);
    setTermId(null);
    setTermHost(null);
    setTermTmux(null);
    onChanged();
    loadSessions(); // drop the killed tmux from "live sessions" right away
  };

  // ── group: several jobs for one task, managed under a representative card ───────────
  const [groupPick, setGroupPick] = useState(false);
  const [groupFilter, setGroupFilter] = useState("");
  const [groupOpen, setGroupOpen] = useState(false); // the header ⧉ group dropdown
  const groupRef = useRef<HTMLDivElement>(null);
  const myGroup = card.cached?.group as string | undefined; // set → this card is a member
  const primaryCard = myGroup ? allCards.find((c) => c.id === myGroup) : undefined;
  const members = allCards.filter((c) => (c.cached?.group as string | undefined) === card.id);
  // close the group dropdown on an outside click (it lives in the header action row)
  useEffect(() => {
    if (!groupOpen) return;
    const onDown = (e: Event) => {
      if (groupRef.current && !groupRef.current.contains(e.target as Node)) {
        setGroupOpen(false);
        setGroupPick(false);
      }
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [groupOpen]);
  const isPrimaryCard = (c: Card) => allCards.some((x) => x.cached?.group === c.id);
  const ballDot = (ball: string) =>
    ball === "human" ? "text-amber-400" : ball === "ai" ? "text-sky-400" : "text-zinc-500";
  const groupInto = (memberId: string, primaryId: string | null) => {
    setGroupPick(false);
    return run(() => groupCard(memberId, primaryId), primaryId ? "added to group" : "removed from group");
  };

  // ── hand off this task to the other machine (Base ⇄ Roam), one action ──────────────
  // inject a prompt so the CURRENT claude writes a handover doc + pushes, poll until the
  // doc lands, then move it to `toHost` and open a fresh claude there seeded to read it +
  // continue. The code travels via git (claude pushed); this only moves the doc.
  const handoverGo = async (toHost: string) => {
    if (!termTmux || handingOff) return;
    const abort = { v: false };
    handoverAbort.current = abort;
    setHandingOff(true);
    try {
      // Resumable: if a handover doc is already sitting on the source (e.g. a prior
      // attempt whose poll timed out before claude finished writing), transfer it
      // straight away — do NOT re-inject, which would clear the finished doc and make
      // claude write it all over again. Only inject when there's nothing to pick up.
      let r = await handoverTransfer(card.id, termHost, toHost || null); // real errors throw
      if (!r.ready) {
        setMsg("handover in progress (claude is writing + pushing) — watch it in the terminal");
        await handoverPrep(card.id, termTmux, termHost); // clears the old doc + injects the prompt
        const deadline = Date.now() + 360_000; // 6 min: writing a full doc + commit + push is slow
        for (;;) {
          await new Promise((res) => setTimeout(res, 3000));
          if (abort.v) break;
          r = await handoverTransfer(card.id, termHost, toHost || null);
          if (r.ready) break;
          if (Date.now() > deadline)
            throw new Error('claude is still writing the handover (or waiting on your answer) — once it finishes, click "hand to" again to take over directly');
        }
      }
      if (abort.v || !r.ready || !r.pickup_prompt) {
        setMsg(abort.v ? "handover cancelled" : "handover not ready yet");
        return;
      }
      const t = await openTerminal(card.id, "own", { host: toHost || undefined, initial_prompt: r.pickup_prompt });
      stopPrevView();
      setTermUrl(t.url || null);
      setTermId(t.id);
      setTermHost(t.host ?? null);
      setTermTmux(t.claude_session_id ? `conductor-${t.claude_session_id.slice(0, 8)}` : null);
      onChanged();
      refreshSessionsSoon();
      setMsg(`✓ picked up on ${toHost || "base"} — remember to push base's code first`);
    } catch (e: any) {
      setMsg(`⚠ ${e?.message || e}`);
    } finally {
      setHandingOff(false);
      handoverAbort.current = null;
    }
  };
  // hand-off items for the terminal's ⋯ menu (only when there's another machine to hand to)
  const handoverMenu =
    hosts.length > 1 && termTmux ? (
      handingOff ? (
        <div className="flex items-center gap-2 px-2 py-1 text-[11px]">
          <span className="text-amber-400 animate-pulse">handover in progress…</span>
          <button
            onClick={() => {
              if (handoverAbort.current) handoverAbort.current.v = true;
            }}
            className="text-zinc-400 hover:text-zinc-200"
          >
            cancel
          </button>
        </div>
      ) : (
        hosts
          .filter((h) => (h.local ? "" : h.name) !== (termHost ?? ""))
          .map((h) => {
            const toHost = h.local ? "" : h.name;
            const remote = !h.local; // host colours: remote = violet, local = cyan (HostChip)
            return (
              <button
                key={h.name}
                disabled={busy || !h.online}
                onClick={() => handoverGo(toHost)}
                className={`flex items-center gap-1.5 px-2 py-1 rounded text-left disabled:opacity-40 ${
                  remote ? "text-violet-300 hover:bg-violet-500/10" : "text-cyan-300 hover:bg-cyan-500/10"
                }`}
                title={`write handover + push → send to ${h.name} → open a fresh claude there to take over (push base's code first)`}
              >
                <span className={remote ? "text-violet-400" : "text-cyan-400"}>●</span>⇄ hand to {h.name}
              </button>
            );
          })
      )
    ) : null;

  // drag the panel's left edge to resize its width
  const startResize = (e: ReactMouseEvent) => {
    e.preventDefault();
    const onMove = (ev: MouseEvent) =>
      onResize(Math.min(1200, Math.max(380, window.innerWidth - ev.clientX)));
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  return (
    <div
      // mobile: full-screen sheet (a 560px side drawer is unusable on a phone,
      // §3F.1). desktop (sm+): resizable right drawer.
      className="fixed inset-0 sm:inset-y-0 sm:left-auto sm:right-0 max-w-full bg-surface-raised sm:border-l border-zinc-800 shadow-2xl flex flex-col z-50 has-[[data-term-maximized]]:z-[60]"
      style={{
        // desktop: fixed-width drawer starting just below the app header (menu bar);
        // mobile: full-screen sheet (width/top come from the inset-0 class).
        width: window.matchMedia("(min-width: 640px)").matches ? width : undefined,
        top: window.matchMedia("(min-width: 640px)").matches ? headerH : undefined,
      }}
    >
      <div
        onMouseDown={startResize}
        className="absolute left-0 top-0 h-full w-1.5 cursor-ew-resize hover:bg-sky-500/40 z-10 hidden sm:block"
        title="drag to resize panel width"
      />
      {/* one scroll owns header + tabs + content, so on MOBILE the header scrolls
          away (reclaims terminal height); on desktop sm:sticky pins it as before. */}
      <div className="flex-1 min-h-0 overflow-y-auto">
      {/* header: pinned on desktop, scrolls with content on mobile. A compact
          HOST (workspace tile) hides it wholesale — title/state/actions live
          in the host's own title bar then (compactHeader prop). */}
      {!compactHeader && (
      <div className="sm:sticky sm:top-0 sm:z-20 bg-surface-raised px-4 py-3 border-b border-zinc-800 space-y-2">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-zinc-700 text-zinc-200 uppercase">
            {card.origin}
          </span>
          {card.cached?.slack?.from ? (
            <span className="text-sm text-zinc-200">{card.cached.slack.from}</span>
          ) : (
            <span className="font-mono text-sm text-zinc-300">{card.external_id}</span>
          )}
          {card.cached?.slack?.ts && (
            <span
              className="text-xs text-zinc-500"
              title={new Date(parseFloat(card.cached.slack.ts) * 1000).toLocaleString()}
            >
              {new Date(parseFloat(card.cached.slack.ts) * 1000).toLocaleString(undefined, {
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
              })}
            </span>
          )}
          {card.url && (
            <a href={card.url} target="_blank" rel="noreferrer" className="text-xs text-blue-400 hover:underline">
              ↗
            </a>
          )}
          <button
            onClick={onClose}
            className="ml-auto text-zinc-500 hover:text-zinc-200 text-lg leading-none"
          >
            ✕
          </button>
        </div>
        <div className="flex items-start gap-2">
          <h2 className="flex-1 min-w-0 text-body font-semibold text-zinc-100 line-clamp-2">{card.title}</h2>
          {card.agent_state && (
            <span className="shrink-0 mt-0.5 flex items-center gap-1.5 text-xs text-zinc-500 max-w-[40%]">
              <span className="truncate">{card.agent_state}</span>
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-1.5 text-xs">
          {(card.origin === "manual" || card.origin === "slack") &&
            ((card.origin === "manual" ? card.cached?.manual?.open === false : card.cached?.slack?.done) ? (
              <Btn variant="ghost" onClick={() => run(() => setDone(card.id, false), "Reopened")}>
                ↩ Reopen
              </Btn>
            ) : (
              <Btn variant="ok" onClick={() => run(() => setDone(card.id, true), "Done")}>
                ✓ Done
              </Btn>
            ))}
          {isSnoozed ? (
            <Btn variant="ghost" onClick={() => run(() => patchState(card.id, { unsnooze: true }), "Un-snoozed")}>
              ⏰ Un-snooze
            </Btn>
          ) : (
            <HoverCard
              width={150}
              content={
                <div className="flex flex-col gap-0.5">
                  <div className="px-1 pb-1 text-[10px] uppercase tracking-wider text-zinc-400">
                    snooze for…
                  </div>
                  {SNOOZE_CHOICES.map((c) => (
                    <button
                      key={c.hours}
                      onClick={() =>
                        run(() => patchState(card.id, { snooze_hours: c.hours }), `Snoozed ${c.label}`)
                      }
                      className="px-2 py-1 rounded text-left text-xs text-zinc-200 hover:bg-zinc-800"
                    >
                      {c.label}
                    </button>
                  ))}
                </div>
              }
            >
              <Btn variant="ghost" onClick={() => run(() => patchState(card.id, { snooze_hours: 4 }), "Snoozed 4h")}>
                💤 Snooze
              </Btn>
            </HoverCard>
          )}
          {card.local.pinned ? (
            <Btn variant="ghost" onClick={() => run(() => patchState(card.id, { pinned: false }), "Unpinned")}>
              📌 Unpin
            </Btn>
          ) : (
            <Btn variant="ghost" onClick={() => run(() => patchState(card.id, { pinned: true }), "Pinned")}>
              📌 Pin
            </Btn>
          )}
          {card.origin === "jira" &&
            (isHeld ? (
              <Btn
                variant="ghost"
                onClick={() => run(() => setHold(card.id, false), "hold removed")}
                title="remove hold"
              >
                ▶ Release hold
              </Btn>
            ) : (
              <Btn
                variant="ghost"
                onClick={() => run(() => setHold(card.id, true), "hold added")}
                title="add hold — park it for a human"
              >
                ⏸ Hold
              </Btn>
            ))}
          <Btn
            variant="ghost"
            onClick={() =>
              run(
                () => patchState(card.id, { dismissed: !card.local.dismissed }),
                card.local.dismissed ? "Restored" : "Dismissed",
              )
            }
          >
            {card.local.dismissed ? "↩ Restore" : "✕ Dismiss"}
          </Btn>
          {/* plugin actions slot — plugins with card_widget slot="actions" render here */}
          <PluginCardActions card={card} />
          <div className="relative ml-auto" ref={groupRef}>
            <button
              onClick={() => setGroupOpen((v) => !v)}
              title="group: collect several jobs under one representative card"
              className={`px-2 py-1 rounded border ${
                myGroup || members.length
                  ? "border-sky-700/60 bg-sky-900/30 text-sky-200"
                  : "border-zinc-700 bg-zinc-800/60 text-zinc-300"
              } hover:bg-zinc-700`}
            >
              ⧉ {myGroup ? "in a group" : `group${members.length ? ` · ${members.length}` : ""}`}
            </button>
            {groupOpen && (
              <div className="absolute right-0 top-full z-30 mt-1 w-72 rounded-lg border border-zinc-700 bg-zinc-900 p-2.5 text-left shadow-2xl">
                {myGroup ? (
                  <div className="flex flex-wrap items-center gap-1.5 text-xs">
                    <span className="text-zinc-500">⧉ in a group · primary:</span>
                    {primaryCard ? (
                      <button
                        onClick={() => {
                          onOpenCard?.(myGroup);
                          setGroupOpen(false);
                        }}
                        className="text-sky-300 hover:underline truncate max-w-[55%]"
                      >
                        {primaryCard.title || primaryCard.external_id}
                      </button>
                    ) : (
                      <span className="text-zinc-400">(primary card not in the list)</span>
                    )}
                    <button
                      onClick={() => groupInto(card.id, null)}
                      className="ml-auto text-zinc-400 hover:text-zinc-200"
                    >
                      ungroup
                    </button>
                  </div>
                ) : (
                  <section className="space-y-1.5">
                    <div className="flex items-center gap-2">
                      <h3 className="text-xs font-semibold text-zinc-300 uppercase tracking-wide">
                        ⧉ group{members.length ? ` · ${members.length}` : ""}
                      </h3>
                      <button
                        onClick={() => setGroupPick((v) => !v)}
                        className="text-[11px] text-sky-300 hover:text-sky-200"
                      >
                        {groupPick ? "cancel" : "＋ add a job to this group"}
                      </button>
                    </div>
                    {members.map((m) => (
                      <div key={m.id} className="flex items-center gap-2 text-xs">
                        <span className={ballDot(m.ball)}>●</span>
                        <button
                          onClick={() => {
                            onOpenCard?.(m.id);
                            setGroupOpen(false);
                          }}
                          className="flex-1 text-left text-zinc-200 truncate hover:underline"
                        >
                          {m.title || m.external_id}
                        </button>
                        <button
                          onClick={() => groupInto(m.id, null)}
                          className="text-zinc-500 hover:text-red-300"
                          title="remove from group"
                        >
                          ✕
                        </button>
                      </div>
                    ))}
                    {groupPick && (
                      <div className="rounded border border-zinc-800 bg-zinc-950/40 p-1.5 space-y-1">
                        <input
                          autoFocus
                          value={groupFilter}
                          onChange={(e) => setGroupFilter(e.target.value)}
                          placeholder="find a job…"
                          // 16px — iOS's no-auto-zoom threshold, and this one autoFocuses,
                          // so at text-xs the page zoomed the instant the picker opened.
                          className="w-full text-base px-2 py-1 rounded bg-zinc-800 border border-zinc-700 text-zinc-100"
                        />
                        <div className="max-h-48 overflow-y-auto space-y-0.5">
                          {allCards
                            .filter(
                              (c) =>
                                c.id !== card.id &&
                                !c.cached?.group &&
                                !isPrimaryCard(c) &&
                                c.lane !== "done",
                            )
                            .filter((c) => {
                              const q = groupFilter.trim().toLowerCase();
                              return !q || `${c.external_id} ${c.title}`.toLowerCase().includes(q);
                            })
                            .slice(0, 40)
                            .map((c) => (
                              <button
                                key={c.id}
                                onClick={() => groupInto(c.id, card.id)}
                                className="block w-full text-left px-2 py-1 rounded text-xs text-zinc-200 hover:bg-zinc-800 truncate"
                              >
                                {c.title || c.external_id}
                              </button>
                            ))}
                        </div>
                      </div>
                    )}
                  </section>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
      )}

      <div className="p-4 space-y-5">
        {msg && (
          <div className="text-xs px-3 py-2 rounded bg-zinc-800 text-zinc-300 border border-zinc-700">{msg}</div>
        )}
        {/* each group: on mobile only the active tab shows; sm+ always shows (all at once) */}
        <div className="space-y-5">
            {/* status (agent_state) now rides the title row in the header */}
            {(card.cached?.agent?.choice || card.cached?.agent?.notification) && (
              <div className="flex items-start gap-1.5 text-sm text-amber-200 bg-amber-500/10 border border-amber-500/25 rounded p-2">
                <span className="shrink-0">🔔</span>
                <span className="whitespace-pre-wrap">
                  {card.cached?.agent?.choice || card.cached?.agent?.notification}
                </span>
              </div>
            )}
            {card.cached?.jira?.is_subtask && card.cached?.jira?.parent_key && (
              <div className="text-xs text-zinc-400">
                ↳ Sub-task of <span className="font-mono text-zinc-300">{card.cached.jira.parent_key}</span>
                {card.cached.jira.parent_summary && (
                  <span className="text-zinc-500"> · {card.cached.jira.parent_summary}</span>
                )}
              </div>
            )}


        {card.cached?.slack?.brief && (
          <FocusBlock id="brief-slack" title="What you need to do" focus={focusBlock} setFocus={setFocusBlock}>
          <section className="space-y-1">
            <h3 className="text-xs font-semibold text-sky-300 uppercase tracking-wide">
              🤖 What you need to do
            </h3>
            <div className="text-sm text-sky-100 bg-sky-500/10 border border-sky-500/20 rounded p-2.5 whitespace-pre-wrap">
              {card.cached.slack.brief}
            </div>
          </section>
          </FocusBlock>
        )}

        {card.cached?.github?.brief && (
          <FocusBlock id="brief-pr" title="PR summary" focus={focusBlock} setFocus={setFocusBlock}>
          <section className="space-y-1">
            <h3 className="text-xs font-semibold text-teal-300 uppercase tracking-wide">
              🤖 PR summary · my review
            </h3>
            {/* The PR brief is markdown by construction — its prompt (prompts.py
                pr_brief) asks for **bold**-labelled bullets with `code` spans — so
                render it, not the raw syntax. */}
            <div className="text-sm text-teal-100 bg-teal-500/10 border border-teal-500/20 rounded p-2.5">
              <Markdown text={card.cached.github.brief} />
            </div>
          </section>
          </FocusBlock>
        )}

        <PluginCardSlot card={card} slot="above-body" focus={focusBlock} setFocus={setFocusBlock} />

        {(() => {
          const isThread = card.origin === "slack" && slackThread.length > 0;
          if (!isThread && !body?.content) return null;
          const title = isThread
            ? `Thread · ${slackThread.length}`
            : body?.kind === "slack"
              ? "Message"
              : body?.kind === "jira"
                ? "Description"
                : "Details";
          return (
            <FocusBlock id="body" title={title} focus={focusBlock} setFocus={setFocusBlock}>
            <section className="space-y-1">
              <button
                onClick={() => setBodyOpen(!bodyOpen)}
                className="flex w-full items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-400 hover:text-zinc-200"
              >
                <span>{title}</span>
                <span className="text-zinc-500">{bodyOpen ? "▾" : "▸"}</span>
              </button>
              {bodyOpen &&
                (isThread ? (
                  <div className="space-y-2 max-h-96 overflow-y-auto rounded bg-zinc-950/40 border border-zinc-800 p-2.5">
                    {slackThread.map((m, i) => (
                      <div
                        key={i}
                        className={`rounded-md px-2.5 py-1.5 border-l-2 ${
                          m.is_me
                            ? "border-sky-500 bg-sky-500/[0.07]"
                            : "border-emerald-500/60 bg-emerald-500/[0.05]"
                        }`}
                      >
                        <div className="mb-0.5 flex items-baseline gap-2">
                          <span
                            className={`text-xs font-semibold ${m.is_me ? "text-sky-300" : "text-emerald-300"}`}
                          >
                            {m.is_me ? "you" : m.user}
                          </span>
                          <span className="text-[10px] text-zinc-500 tabular-nums">
                            {new Date(parseFloat(m.ts) * 1000).toLocaleString(undefined, {
                              month: "2-digit",
                              day: "2-digit",
                              hour: "2-digit",
                              minute: "2-digit",
                            })}
                          </span>
                        </div>
                        <div className="whitespace-pre-wrap text-sm leading-relaxed text-zinc-200">
                          {m.text}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-sm text-zinc-300 max-h-72 overflow-y-auto rounded bg-zinc-950/40 border border-zinc-800 p-2.5">
                    {/* Only render markdown where the producer actually emits it. Slack
                        has its own mrkdwn dialect (single-* bold), a github body is the PR
                        TITLE, and a plugin's card_body carries an arbitrary kind — running
                        any of those through the parser reformats text nobody marked up. */}
                    {body?.kind === "jira" || body?.kind === "markdown" ? (
                      <Markdown text={body?.content || ""} />
                    ) : (
                      <div className="whitespace-pre-wrap">{body?.content}</div>
                    )}
                  </div>
                ))}
            </section>
            </FocusBlock>
          );
        })()}

        <PluginCardSlot card={card} slot="below-body" focus={focusBlock} setFocus={setFocusBlock} />

        {isHold && (
          <FocusBlock id="awaiting" title="Awaiting Input" focus={focusBlock} setFocus={setFocusBlock}>
          <section className="space-y-2">
            <button
              onClick={() => setAwaitingOpen(!awaitingOpen)}
              className="flex w-full items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-amber-300 hover:text-amber-200"
            >
              <span>Awaiting Input</span>
              <span className="text-amber-500/70">{awaitingOpen ? "▾" : "▸"}</span>
            </button>
            {awaitingOpen && (
              <>
                {awaiting ? (
                  <>
                    {/* The workpad question is markdown — resume.py fetches the comment's
                        ADF and runs it through _adf_to_markdown — so render it, not the
                        raw syntax. A real workpad is a full document whose actionable
                        "⏭️ Next action" ask sits at the very bottom, so show it whole in a
                        scrollable box rather than truncating the ask away. */}
                    <div className="text-sm text-zinc-200 max-h-96 overflow-y-auto rounded bg-zinc-950/40 border border-zinc-800 p-2.5">
                      <Markdown text={awaiting.question} />
                    </div>
                    <div className="flex flex-col gap-1.5">
                      {awaiting.options.map((opt, i) => (
                        <button
                          key={i}
                          disabled={busy}
                          onClick={() =>
                            run(() => resumeCard(card.id, opt), "Answer posted · resuming")
                          }
                          className="text-left text-sm px-3 py-2 rounded bg-amber-500/10 hover:bg-amber-500/20 border border-amber-500/30 text-amber-100 disabled:opacity-50"
                        >
                          {opt}
                        </button>
                      ))}
                    </div>
                  </>
                ) : (
                  <p className="text-xs text-zinc-500">
                    No structured question parsed — answer free-form below.
                  </p>
                )}
                <div className="flex gap-2">
                  <input
                    value={answer}
                    onChange={(e) => setAnswer(e.target.value)}
                    placeholder="custom answer…"
                    className="flex-1 text-sm px-2 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-zinc-100"
                  />
                  <button
                    disabled={busy || !answer.trim()}
                    onClick={() =>
                      run(() => resumeCard(card.id, answer), "Answer posted · resuming")
                    }
                    className="text-sm px-3 py-1.5 rounded bg-amber-500/80 hover:bg-amber-500 text-zinc-900 font-medium disabled:opacity-40"
                  >
                    Send
                  </button>
                </div>
              </>
            )}
          </section>
          </FocusBlock>
        )}

        {isPR && (
          <FocusBlock id="pr" title="Pull Request" focus={focusBlock} setFocus={setFocusBlock}>
          <section className="space-y-2">
            <div className="flex items-center gap-2">
              <h3 className="text-xs font-semibold text-purple-300 uppercase tracking-wide">
                Pull Request
              </h3>
              {prHasDetail && (
                <button
                  onClick={() => setPrDetailOpen(!prDetailOpen)}
                  className="text-[11px] text-zinc-400 hover:text-zinc-200"
                  title="toggle: full reviewer/CI detail ↔ repo#id summary only"
                >
                  {prDetailOpen ? "summary only ▴" : "details ▾"}
                </button>
              )}
            </div>

            {/* summary line — always shown: repo#id (→ GitHub) · CI · review decision */}
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
              {card.url || gh.url ? (
                <a
                  href={card.url || gh.url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-mono text-blue-400 hover:underline"
                >
                  {gh.repo}#{gh.number} ↗
                </a>
              ) : (
                <span className="font-mono text-zinc-300">
                  {gh.repo}#{gh.number}
                </span>
              )}
              {gh.author && <span className="text-zinc-400">· by {gh.author}</span>}
              {gh.ci && gh.ci !== "none" && <span className={ciCls(gh.ci)}>· CI {gh.ci}</span>}
              {gh.review && (
                <span className="text-zinc-400">· {String(gh.review).replace(/_/g, " ")}</span>
              )}
            </div>

            {prDetailOpen && prHasDetail && (
              <div className="space-y-2.5 rounded bg-zinc-950/40 border border-zinc-800 p-2.5">
                {(prHumans.length > 0 || prReqs.length > 0) && (
                  <div className="space-y-1">
                    <div className="text-[10px] text-zinc-500 uppercase tracking-wide">👤 Reviewers</div>
                    <div className="flex flex-wrap gap-1.5">
                      {prHumans.map((r) => {
                        const st = PR_REVIEW[r.state] || PR_REVIEW.commented;
                        return (
                          <span key={r.user} className={`text-xs px-1.5 py-0.5 rounded border ${st.cls}`}>
                            {st.icon} {r.user}
                          </span>
                        );
                      })}
                      {prReqs.map((u) => (
                        <span
                          key={u}
                          className="text-xs px-1.5 py-0.5 rounded border text-zinc-400 border-zinc-600 bg-zinc-700/30"
                        >
                          ⏳ {u}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {prAi.length > 0 && (
                  <div className="space-y-1">
                    <div className="text-[10px] text-zinc-500 uppercase tracking-wide">🤖 AI agents</div>
                    <div className="flex flex-wrap gap-1.5">
                      {prAi.map((r) => {
                        const st = PR_REVIEW[r.state] || PR_REVIEW.commented;
                        return (
                          <span key={r.user} className={`text-xs px-1.5 py-0.5 rounded border ${st.cls}`}>
                            {st.icon} {r.user}
                          </span>
                        );
                      })}
                    </div>
                  </div>
                )}
                {prChecks.length > 0 && (
                  <div className="space-y-1">
                    <div className="text-[10px] text-zinc-500 uppercase tracking-wide">
                      CI · {prChecks.filter((c) => c.state === "failing").length} failing ·{" "}
                      {prChecks.filter((c) => c.state === "pending").length} pending
                    </div>
                    <div className="flex flex-col gap-1">
                      {prChecks.map((c, i) => {
                        const fail = c.state === "failing";
                        const cls = `flex items-center gap-1.5 text-xs ${fail ? "text-red-300" : "text-amber-200"}`;
                        const body = (
                          <>
                            <span>{fail ? "✗" : "⏳"}</span>
                            <span className="truncate">{c.name}</span>
                          </>
                        );
                        return c.url ? (
                          <a key={i} href={c.url} target="_blank" rel="noreferrer" className={`${cls} hover:underline`}>
                            {body}
                          </a>
                        ) : (
                          <div key={i} className={cls}>
                            {body}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            )}

            <div className="flex gap-2">
              <button
                disabled={busy}
                onClick={() => run(() => prAction(card.id, "approve"), "Approved")}
                className="text-sm px-3 py-1.5 rounded bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/30 text-emerald-200 disabled:opacity-50"
              >
                Approve
              </button>
              <button
                disabled={busy}
                onClick={() => run(() => prAction(card.id, "merge"), "Merge requested")}
                className="text-sm px-3 py-1.5 rounded bg-purple-500/15 hover:bg-purple-500/25 border border-purple-500/30 text-purple-200 disabled:opacity-50"
              >
                Merge
              </button>
            </div>
          </section>
          </FocusBlock>
        )}

          </div>

        {/* the terminal/sessions block: `grow` — focused, it stretches the
            terminal to the card's full height (fill mode) instead of scrolling */}
        <FocusBlock
          id="terminal"
          title="Sessions"
          grow
          focus={focusBlock}
          setFocus={setFocusBlock}
          // merged row: the sessions accordion toggle rides the SAME breadcrumb
          // row, right-aligned — never a second stacked header row
          breadcrumbExtra={
            <button
              onClick={() => setTermChromeOpen((v) => !v)}
              className="flex items-center gap-1.5 px-2 py-0.5 rounded text-xs bg-zinc-800/70 border border-zinc-700 text-zinc-300 hover:text-zinc-100 hover:bg-zinc-700/70"
              title={termChromeOpen ? "收合工作階段控制" : "展開工作階段控制(新開/切換/清單…)"}
            >
              <span>工作階段</span>
              {(termId || primaryConv) && (
                <span className="font-mono text-[11px] text-zinc-500">
                  claude {(termId || primaryConv?.claude_session_id || "").slice(0, 8)}
                </span>
              )}
              <span className="text-zinc-500">{termChromeOpen ? "⌃" : "⌄"}</span>
            </button>
          }
        >
        <section
          className={
            termFocused
              ? "flex-1 min-h-0 flex flex-col gap-2"
              : "space-y-2 sm:border-t sm:border-zinc-800 sm:pt-4"
          }
        >
          {(!termFocused || termChromeOpen) && (
          <>
          <div className="flex flex-wrap gap-2">
            {!!card.cached?.watch && (
              <button
                disabled={busy}
                onClick={() => doTerminal("attach")}
                className="text-xs px-2.5 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 disabled:opacity-50"
                title="watch the plugin-declared live turn (only while it's running)"
              >
                Watch
              </button>
            )}
            <button
              disabled={busy}
              onClick={() => setNewOpen((o) => !o)}
              className={`text-xs px-2.5 py-1.5 rounded border disabled:opacity-50 ${
                newOpen
                  ? "bg-sky-500/30 border-sky-400/50 text-sky-100"
                  : "bg-sky-500/15 hover:bg-sky-500/25 border-sky-500/30 text-sky-200"
              }`}
            >
              New Claude session {newOpen ? "▴" : "▾"}
            </button>
            {primaryConv && (
              <div className="relative inline-flex">
                <button
                  disabled={busy}
                  onClick={() => resumeConv(primaryConv)}
                  className={`text-xs px-2.5 py-1.5 ${convs.length > 1 ? "rounded-l" : "rounded"} bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/30 text-emerald-200 disabled:opacity-50`}
                  title={`claude --resume ${primaryConv.claude_session_id}${primaryConv.host ? ` (on ${primaryConv.host})` : ""}${primaryConv.live ? " · tmux still alive" : " · tmux ended — resume reopens it"}`}
                >
                  <span className={primaryConv.live ? "text-emerald-400" : "text-zinc-500"}>
                    {primaryConv.live ? "●" : "○"}
                  </span>{" "}
                  Resume {primaryConv.claude_session_id.slice(0, 8)}
                  {primaryConv.host && (
                    <span className="ml-1 text-[9px] px-1 py-px rounded bg-violet-500/20 text-violet-300 border border-violet-500/30">
                      {primaryConv.host}
                    </span>
                  )}
                </button>
                {convs.length > 1 && (
                  <>
                    <button
                      disabled={busy}
                      onClick={() => setResumeMenu((o) => !o)}
                      className="text-xs px-1.5 py-1.5 rounded-r bg-emerald-500/15 hover:bg-emerald-500/25 border border-l-0 border-emerald-500/30 text-emerald-200 disabled:opacity-50"
                      title={`${convs.length} sessions resumable (including killed ones)`}
                    >
                      ▾
                    </button>
                    {resumeMenu && (
                      <div className="absolute left-0 top-full mt-1 z-30 min-w-[15rem] rounded-lg bg-zinc-900 border border-zinc-700 shadow-xl p-1 space-y-0.5">
                        {convs.map((c) => (
                          <button
                            key={c.claude_session_id}
                            disabled={busy}
                            onClick={() => {
                              setResumeMenu(false);
                              resumeConv(c);
                            }}
                            className="w-full text-left text-xs px-2 py-1.5 rounded hover:bg-zinc-800 disabled:opacity-50 flex items-center gap-1.5"
                            title={`claude --resume ${c.claude_session_id}${c.live ? " · tmux still alive" : " · tmux ended — resume reopens it"}`}
                          >
                            <span className={c.live ? "text-emerald-400" : "text-zinc-600"}>
                              {c.live ? "●" : "○"}
                            </span>
                            <span className="font-mono">{c.claude_session_id.slice(0, 8)}</span>
                            <HostChip host={c.host} localName={localName} />
                            {c.last_used && (
                              <span className="ml-auto text-[10px] text-zinc-500">
                                {new Date(c.last_used).toLocaleString("zh-TW", {
                                  month: "numeric", day: "numeric",
                                  hour: "2-digit", minute: "2-digit",
                                })}
                              </span>
                            )}
                          </button>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
          {newOpen && (
            <div className="space-y-2 rounded-lg border border-sky-500/30 bg-sky-500/5 p-2.5">
              {/* launch profiles as pills (matches the "run on" row) — shown only when
                  the user has defined some; no "none" chip, an unselected row IS none,
                  and clicking the active pill again clears it. */}
              {profiles.length > 0 && (
                <div className="flex items-center gap-1.5 text-[11px] flex-wrap">
                  <span className="text-zinc-500">profile</span>
                  {profiles.map((p) => {
                    const selected = profile === p.name;
                    return (
                      <button
                        key={p.name}
                        disabled={busy}
                        onClick={() => {
                          if (selected) {
                            setProfile(""); // toggle off → no profile (env not injected)
                            return;
                          }
                          // selecting fills the visible fields; env rides the launch by name
                          setProfile(p.name);
                          if (p.cwd != null) setCwd(p.cwd);
                          const h = hosts.find((x) => x.name === p.host);
                          setSelHost(h && !h.local ? h.name : "");
                        }}
                        title={
                          [
                            [p.host && `host: ${p.host}`, p.cwd && `cwd: ${p.cwd}`]
                              .filter(Boolean)
                              .join(" · "),
                            Object.entries(p.env ?? {}).length
                              ? `env:\n${Object.entries(p.env)
                                  .map(([k, v]) => `  ${k}=${v}`)
                                  .join("\n")}`
                              : "",
                          ]
                            .filter(Boolean)
                            .join("\n") || p.name
                        }
                        className={`px-2 py-0.5 rounded border disabled:opacity-40 ${
                          selected
                            ? "bg-sky-500/20 border-sky-500/50 text-sky-200"
                            : "bg-zinc-800 border-zinc-700 text-zinc-300 hover:bg-zinc-700"
                        }`}
                      >
                        {p.name}
                      </button>
                    );
                  })}
                </div>
              )}
              <div className="relative">
                <input
                  value={cwd}
                  onChange={(e) => setCwd(e.target.value)}
                  onFocus={() => setCwdFocus(true)}
                  onBlur={() => setCwdFocus(false)}
                  onKeyDown={(e) => {
                    // Tab accepts the ghost completion (Shift+Tab still moves focus away)
                    if (e.key === "Tab" && !e.shiftKey && cwdGhost) {
                      e.preventDefault();
                      setCwd(cwdGhost);
                    }
                  }}
                  placeholder="working dir (blank → configured default)"
                  list="cwd-dirs"
                  className={`relative z-10 w-full text-xs px-2 py-1.5 rounded bg-zinc-800 border text-zinc-100 font-mono ${
                    cwd.trim() && dirHint.valid === false
                      ? "border-amber-500/60"
                      : cwd.trim() && dirHint.valid
                        ? "border-emerald-600/50"
                        : "border-zinc-700"
                  }`}
                />
                {/* ghost completion: an INVISIBLE copy of the typed text reserves its exact
                    width, then the first suggestion's remainder shows grey — press Tab to
                    accept it. border-transparent matches the input's 1px border so the grey
                    suffix lines up right after the typed text. */}
                {cwdGhost && (
                  <>
                    <div className="pointer-events-none absolute inset-0 z-20 flex items-center overflow-hidden whitespace-pre rounded border border-transparent py-1.5 pl-2 pr-14 text-xs font-mono">
                      <span className="invisible">{cwd}</span>
                      <span className="text-zinc-500">{cwdGhost.slice(cwd.length)}</span>
                    </div>
                    <span className="pointer-events-none absolute right-2 top-1/2 z-20 -translate-y-1/2 rounded bg-zinc-700 px-1 py-0.5 text-[9px] text-zinc-300">
                      ⇥ tab
                    </span>
                  </>
                )}
              </div>
              {/* the full dropdown of host completions (kept alongside the inline ghost) */}
              <datalist id="cwd-dirs">
                {dirHint.dirs.map((d) => (
                  <option key={d} value={d} />
                ))}
              </datalist>
              {cwd.trim() && dirHint.valid !== null && (
                <div className="text-[11px]">
                  {dirHint.valid ? (
                    <span className="text-emerald-500">
                      ✓ exists on {selHost || hosts.find((h) => h.local)?.name || "base"}
                    </span>
                  ) : (
                    <span className="text-amber-500">
                      ⚠ not found on {selHost || hosts.find((h) => h.local)?.name || "base"}
                    </span>
                  )}
                </div>
              )}
              {/* the selected profile's env, shown inline (a native title tooltip is too
                  hidden) so you can SEE what will be injected before you start. */}
              {(() => {
                const envPairs = Object.entries(
                  profiles.find((x) => x.name === profile)?.env ?? {},
                );
                return envPairs.length ? (
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11px] font-mono">
                    <span className="font-sans text-zinc-500">env</span>
                    {envPairs.map(([k, v]) => (
                      <span key={k} className="whitespace-nowrap text-zinc-400">
                        {k}=<span className="text-zinc-200">{v}</span>
                      </span>
                    ))}
                  </div>
                ) : null;
              })()}
              {hosts.length > 1 && (
                <div className="flex items-center gap-1.5 text-[11px] flex-wrap">
                  <span className="text-zinc-500">run on</span>
                  {hosts.map((h) => {
                    const selected = selHost === "" ? h.local : selHost === h.name;
                    return (
                      <button
                        key={h.name}
                        disabled={busy || (!h.local && !h.online)}
                        onClick={() => setSelHost(h.local ? "" : h.name)}
                        title={
                          h.local
                            ? "this machine (runs Conductor)"
                            : h.online
                              ? "remote machine via ssh"
                              : "offline — unreachable over ssh"
                        }
                        className={`px-2 py-0.5 rounded border disabled:opacity-40 ${
                          selected
                            ? h.local
                              ? "bg-cyan-500/20 border-cyan-500/50 text-cyan-200"
                              : "bg-violet-500/25 border-violet-400/50 text-violet-200"
                            : "bg-zinc-800 border-zinc-700 text-zinc-300 hover:bg-zinc-700"
                        }`}
                      >
                        <span className={h.online ? "text-emerald-400" : "text-zinc-600"}>●</span>{" "}
                        {h.name}
                      </button>
                    );
                  })}
                </div>
              )}
              <div className="flex gap-2">
                <button
                  disabled={busy}
                  onClick={() => {
                    setNewOpen(false);
                    doTerminal("own");
                  }}
                  className="text-xs px-3 py-1.5 rounded bg-sky-600 hover:bg-sky-500 text-white disabled:opacity-50"
                >
                  ▶ start
                </button>
                <button
                  onClick={() => setNewOpen(false)}
                  className="text-xs px-3 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-300"
                >
                  cancel
                </button>
              </div>
            </div>
          )}
          {sessions.length > 0 && (
            <div className="space-y-0">
              <div className="flex items-center gap-2 text-[11px] text-zinc-500 mb-1">
                {/* collapsing hides the tabs AND the pane below — the terminal is the
                    tallest thing on a card, and on a card you are reading rather than
                    driving it pushes everything else off screen. The count stays
                    visible while collapsed so it is still obvious a session is live. */}
                <button
                  onClick={() => setTermOpen(!termOpen)}
                  className="flex items-center gap-1.5 hover:text-zinc-300"
                >
                  <span>live sessions ({sessions.length})</span>
                  <span>{termOpen ? "▾" : "▸"}</span>
                </button>
                <button
                  onClick={loadSessions}
                  className="px-1.5 rounded bg-zinc-800 hover:bg-zinc-700"
                  title="refresh"
                >
                  ↻
                </button>
              </div>
              {/* session tabs: the highlighted one is what the terminal below shows */}
              <div className={`flex flex-wrap gap-1 -mb-px relative z-10 ${termOpen ? "" : "hidden"}`}>
                {sessions.map((s) => {
                  const active =
                    s.name === termTmux && (s.host ?? null) === (termHost ?? null) && !!termUrl;
                  return (
                    <button
                      key={`${s.host || "local"}:${s.name}`}
                      disabled={busy}
                      onClick={() => openSession(s)}
                      className={`text-xs px-2.5 py-1.5 rounded-t border disabled:opacity-50 ${
                        active
                          ? "bg-zinc-950 border-zinc-600 border-b-zinc-950 text-zinc-100 font-medium"
                          : "bg-zinc-800/60 hover:bg-zinc-800 border-zinc-700/60 border-b-zinc-600 text-zinc-400"
                      }`}
                      title={
                        (s.host ? `${s.name} on ${s.host}` : s.name) +
                        (active ? " · currently shown" : " · click to switch")
                      }
                    >
                      <span
                        className={s.kind === "conductor" ? "text-emerald-400" : "text-zinc-500"}
                      >
                        ●
                      </span>{" "}
                      {s.kind === "conductor"
                        ? `claude ${s.name.replace("conductor-", "")}`
                        : s.name}{" "}
                      <HostChip host={s.host} localName={localName} />
                      {s.attached && <span className="text-[9px] text-zinc-500"> ·attached</span>}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          </>
          )}
          {/* the pane hides only when the toggle that reopens it is actually on screen —
              that toggle lives in the session-list header, so with an empty list a
              collapsed pane would be unreachable */}
          {termUrl && (
            <div
              className={`${termOpen || sessions.length === 0 ? "" : "hidden"} ${
                termFocused ? "flex-1 min-h-0 relative flex flex-col" : ""
              }`}
            >
            <TerminalSurface
              fill={termFocused}
              url={termUrl}
              sid={termId}
              host={termHost}
              fontSize={fontSize}
              onZoom={(s) => {
                setFontSize(s);
                doTerminal(reopenKind(), s); // reopen at the new size (watch-aware)
              }}
              onClose={closeTerminal}
              onDead={() => doTerminal(reopenKind(), fontSize)}
              onKill={killTerm}
              headerExtra={<HostChip host={termHost} localName={localName} />}
              menuExtra={handoverMenu}
              card={card}
            />
            </div>
          )}
        </section>
        </FocusBlock>

        <div className="space-y-5 sm:border-t sm:border-zinc-800 sm:pt-4">
          <PluginCardWidgets card={card} focus={focusBlock} setFocus={setFocusBlock} />
          <FocusBlock id="links" title="Links" focus={focusBlock} setFocus={setFocusBlock}>
          <section className="space-y-2">
          <h3 className="text-xs font-semibold text-zinc-300 uppercase tracking-wide">Links</h3>
          <LinksPanel card={card} slackThread={slackThread} />
          <div className="flex gap-2">
            <input
              value={linkUrl}
              onChange={(e) => setLinkUrl(e.target.value)}
              placeholder="PROJ-123, a PR URL, or any link…"
              // 16px (text-base) is iOS's no-auto-zoom threshold — the same reason
              // TermKeys' input carries it. At text-xs, focusing this on a phone zoomed
              // the page and pushed the card out of view.
              className="flex-1 text-base px-2 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-zinc-100"
            />
            <button
              disabled={busy || !linkUrl.trim()}
              onClick={async () => {
                await run(() => addLink(card.id, mkLink(linkUrl)), "link added");
                setLinkUrl("");
              }}
              className="text-xs px-3 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 disabled:opacity-40"
            >
              Add
            </button>
          </div>
        </section>
        </FocusBlock>

        {/* less-common local action (Done/Snooze/Pin/Dismiss live in the header) */}
        <section className="flex flex-wrap gap-2 text-xs">
          <button
            onClick={() => run(() => patchState(card.id, { read: !card.local.read }), "")}
            className="px-2.5 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-300"
          >
            {card.local.read ? "Mark unread" : "Mark read"}
          </button>
        </section>

        {/* Raw */}
        <section>
          <button
            onClick={() => setShowRaw((v) => !v)}
            className="text-[11px] text-zinc-500 hover:text-zinc-300"
          >
            {showRaw ? "▾" : "▸"} raw cached
          </button>
          {showRaw && (
            <pre className="mt-1 text-[10px] text-zinc-400 bg-zinc-950 rounded p-2 overflow-x-auto border border-zinc-800">
              {JSON.stringify(card.cached, null, 2)}
            </pre>
          )}
        </section>
          </div>
      </div>
      </div>
    </div>
  );
}
