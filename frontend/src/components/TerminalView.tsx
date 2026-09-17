import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { captureTerminal, pasteTerminalImage, scrollTerminal } from "../api";
import { THEME_EVENT } from "../theme";
import { layoutRegistries } from "../kernel/seams";
import type { Card } from "../types";
import { hostFrameClass } from "./HostChip";
import { usePluginWidgets } from "./PluginCardWidgets";
import { TermKeys, hotkeySeq, injectText, loadHotkeys } from "./TermKeys";

// 40px of travel per line ⇒ a standard 120px wheel notch moves 3 lines, the same
// convention the OS and xterm use.
const GESTURE_LINE_PX = 40;
const MAX_LINES_PER_REQUEST = 60; // a fling drains across requests, not in one jump
const WHEEL_LINE_PX = 16; // deltaMode=1 (line-based wheel) → px
const WHEEL_PAGE_PX = 400; // deltaMode=2 (page-based wheel) → px
// Ceiling for the alt-pane page counter, purely a runaway guard. It counts pages we SENT,
// not pages the app moved, so the two errors are NOT symmetric and the cap must never be
// the tighter one: over-counting spends a few PageDowns on a live tail, where they are
// no-ops for these TUIs, while under-counting zeroes the counter with the app still paged
// up and the "already live" gate then refuses every further down — a dead wheel. So this
// sits far above any depth a real scrollback can reach (~40k lines at a typical page)
// rather than near a plausible one: it bounds a stuck wheel out of existence and still
// keeps the counter from growing without bound.
const ALT_PAGES_MAX = 1000;
// Opt back into routing the wheel through the server (the pre-native behaviour):
//   localStorage.setItem("conductor.wheelServerSide", "true")  — then reload.
const WHEEL_SERVER_SIDE = (() => {
  try {
    return JSON.parse(localStorage.getItem("conductor.wheelServerSide") || "false") === true;
  } catch {
    return false;
  }
})();

// Shared terminal viewer: the ttyd iframe + a toolbar (maximize, font zoom, new
// tab, optional kill/close) + the mobile soft-key bar. Used by both the card
// drawer and the Terminals view so they stay in sync.
export function TerminalView({
  url,
  sid,
  host,
  fontSize = 14,
  fill = false,
  onZoom,
  onClose,
  onDead,
  onKill,
  headerExtra,
  menuExtra,
  card,
  hideHeader = false,
}: {
  url: string;
  sid: string | null;
  host?: string | null; // frame accent: violet = remote, cyan = local
  fontSize?: number;
  fill?: boolean; // fill the parent (Terminals view) vs fixed height (card drawer)
  onZoom?: (size: number) => void;
  onClose?: () => void;
  onDead?: () => void; // ttyd dead (proxy "terminal ended") → let the parent respawn it
  onKill?: () => void;
  headerExtra?: ReactNode;
  menuExtra?: ReactNode; // extra items for the ⋯ menu (e.g. hand-off), tucked out of the row
  // hide the toolbar row entirely — for hosts that fold it behind their own
  // accordion (CardDetail's focused-sessions mode). Ignored while maximized:
  // the ⤡ exit button must stay reachable. Kill/Close/copy/font reappear
  // whenever the host un-hides.
  hideHeader?: boolean;
  // the card this terminal belongs to (if any) — the ⋯ menu renders every plugin's
  // slot="menu" row for it HERE, so plugin verbs appear identically wherever a
  // terminal renders (card drawer, Terminals tab), not per call site.
  card?: Card | null;
}) {
  const [maximized, setMaximized] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false); // the ⋯ overflow menu (new tab, hand off)
  // NARROW toolbar (a tile / a squeezed drawer): below this width the full
  // labels wrap the row onto two lines, so buttons go icon-only (titles keep
  // the words) and the font-size label + host chip fold into the ⋯ menu.
  // Width-observed on the row itself (the app's usual ResizeObserver pattern
  // — no container-query plugin dependency); pure conditional render, no
  // behavior change, and a comfortable drawer keeps today's labels.
  const toolbarRef = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const el = toolbarRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setNarrow(el.clientWidth < 620));
    ro.observe(el);
    return () => ro.disconnect();
    // re-attach when the row (un)mounts — a hideHeader host renders it later
  }, [hideHeader, maximized]);
  // plugin ⋯-menu rows, fetched at terminal mount (not first menu open) → zero pop-in
  const pluginMenuRows = usePluginWidgets(card ?? null, "menu");
  // resolved through the kernel seam (core provider = the glob-merged maps)
  const { CARD_LAYOUTS } = layoutRegistries();
  const [keysOpen, setKeysOpen] = useState(false);
  const [copyText, setCopyText] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [vvh, setVvh] = useState<number | null>(null); // visual-viewport height when the keyboard is up
  const [pasting, setPasting] = useState(false);
  const [pasteMsg, setPasteMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [linkState, setLinkState] = useState<"ok" | "retrying" | "lost">("ok");
  const [ready, setReady] = useState(false); // ttyd loaded + settled → fade the iframe in
  const [linesUp, setLinesUp] = useState(0); // how far back in the scrollback we are, per tmux
  const [inMode, setInMode] = useState(false); // tmux copy-mode, which is what pauses typing
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const copiedTimer = useRef<ReturnType<typeof setTimeout>>();
  const revealTimer = useRef<ReturnType<typeof setTimeout>>();
  const respawnedRef = useRef(false); // guard: auto-respawn a dead ttyd at most once per life
  const linesUpRef = useRef(0); // same as linesUp, readable from the iframe listeners
  const gestureRef = useRef(0); // gesture travel not yet spent on a line, in px
  const pendingRef = useRef(0); // lines wanted but not yet requested (− = up)
  const scrollingRef = useRef(false); // a scroll request is in flight
  const eraRef = useRef(0); // bumped per session, so a late reply can't touch the next one
  const exitingRef = useRef(false); // an exit request is in flight; keep intercepting input
  const inModeRef = useRef(false); // copy-mode per tmux — the depth alone can read 0 inside it
  const wantLiveRef = useRef(false); // an exit asked for while a scroll was in flight
  const paneAltRef = useRef<boolean | null>(null); // pane on the alt screen (full-screen app)? null = unknown
  const altPagesRef = useRef(0); // pages we've sent a full-screen app; its depth, since tmux reports none
  const sidRef = useRef(sid); // listeners are bound once per iframe load; read sid live
  sidRef.current = sid;

  // ── scrollback ───────────────────────────────────────────────────────────────
  // Nothing in the browser can scroll this terminal. xterm's viewport has no
  // history to move: tmux occupies the ALT buffer, which never keeps scrollback.
  // And the wheel doesn't help — under tmux's alternate-scroll mode xterm turns it
  // into ↑/↓, which reach claude as prompt-history navigation, hence its own
  // "Scroll wheel is sending arrow keys · use PgUp/PgDn to scroll" (that advice is
  // about the terminal's OWN scrollback, which tmux has taken over; PgUp does
  // nothing here). The scrollback is tmux's, so scrolling means tmux copy-mode,
  // driven server-side — and since copy-mode swallows keystrokes as its own
  // commands, how deep we are has to be visible and easy to leave.
  //
  // Gestures coalesce instead of firing per line: travel accumulates into `pending`,
  // one request carries the whole batch, and whatever arrives mid-flight goes out
  // right after. One redraw per gesture, and no travel is ever dropped — sending a
  // line at a time made a flick lag behind the finger.
  const flushScroll = () => {
    const s = sidRef.current;
    if (!s || scrollingRef.current || !pendingRef.current) return;
    const want = Math.max(-MAX_LINES_PER_REQUEST, Math.min(MAX_LINES_PER_REQUEST, pendingRef.current));
    pendingRef.current -= want;
    scrollingRef.current = true;
    const era = eraRef.current; // this request belongs to THIS session
    // tmux is the authority on where we ended up: it clamps at the oldest line, and
    // counting our own requests would drift past the top and strand the badge
    scrollTerminal(s, want < 0 ? "up" : "down", Math.abs(want))
      .then((r) => {
        if (era !== eraRef.current) {
          // the view moved on; this reply describes a pane we no longer show — and it
          // may have just put that pane in copy-mode, so hand it back to the prompt
          if (r.in_mode) scrollTerminal(s, "exit").catch(() => {});
          return;
        }
        paneAltRef.current = r.alt ?? false;
        inModeRef.current = r.in_mode;
        setInMode(r.in_mode);
        linesUpRef.current = r.in_mode ? r.scroll : 0;
        setLinesUp(linesUpRef.current);
        // A full-screen app reports no depth (tmux keeps none for it), so track our own:
        // the server pages exactly once per request, so this reply moved it one page in
        // the direction we asked. It is what re-arms the "already live" gate below —
        // without it every wheel-down at claude's live tail would inject another PageDown.
        if (r.alt)
          altPagesRef.current = Math.min(ALT_PAGES_MAX, Math.max(0, altPagesRef.current + (want < 0 ? 1 : -1)));
        else altPagesRef.current = 0;
        // Back at the live tail — copy-mode left, or the alt app paged all the way down
        // — so nothing is left to give back and the rest of a fling is dropped.
        if (r.alt ? !altPagesRef.current && want > 0 : !r.in_mode) pendingRef.current = 0;
      })
      .catch(() => {
        // The request may have entered copy-mode before its reply was lost. Leaving the
        // flags at zero would hide the badge, stop intercepting input AND let teardown
        // skip the exit — the silent-dead-terminal shape. Assume the worst so the way
        // out is on screen. Not on a known full-screen pane though: that one is never in
        // tmux copy-mode, so assuming it is would light the badge and eat every keystroke
        // meant for claude — and `toLive`'s own catch would keep restoring that state.
        if (era !== eraRef.current || paneAltRef.current === true) return;
        inModeRef.current = true;
        setInMode(true);
      })
      .finally(() => {
        if (era !== eraRef.current) return;
        scrollingRef.current = false;
        // an exit that arrived mid-flight goes now — its reply is the authoritative one
        if (wantLiveRef.current) {
          wantLiveRef.current = false;
          toLive();
        } else {
          flushScroll();
        }
      });
  };

  // Leaving copy-mode is a round trip too, so input stays intercepted until tmux
  // confirms. Clearing the depth up front would let every keystroke typed during the
  // trip through — a burst, or any normal cadence against a remote host — straight
  // into copy-mode, where they act as commands and are lost.
  // Copy-mode is the thing that pauses typing, and tmux can be in it while reporting a
  // depth of 0 — so the flag decides, never the number.
  const scrolledBack = () => inModeRef.current || linesUpRef.current > 0 || exitingRef.current;

  const toLive = () => {
    gestureRef.current = 0;
    pendingRef.current = 0;
    const s = sidRef.current;
    if (!s || exitingRef.current || !scrolledBack()) return; // idempotent while in flight
    if (scrollingRef.current) {
      // A scroll is in flight and its reply would land after ours, restoring the state
      // we are about to clear. Queue behind it instead of racing it; input stays
      // intercepted meanwhile because copy-mode is still flagged.
      wantLiveRef.current = true;
      return;
    }
    const was = linesUpRef.current;
    const era = eraRef.current;
    const wasInMode = inModeRef.current;
    exitingRef.current = true;
    scrollingRef.current = true; // a queued scroll must not race the exit
    linesUpRef.current = 0;
    inModeRef.current = false;
    setLinesUp(0);
    setInMode(false);
    scrollTerminal(s, "exit")
      .then((r) => {
        // tmux says it's still in copy-mode: keep the way out on screen
        if (era === eraRef.current && r.in_mode) {
          inModeRef.current = true;
          setInMode(true);
          linesUpRef.current = r.scroll || was;
          setLinesUp(linesUpRef.current);
        }
      })
      .catch(() => {
        if (era !== eraRef.current) return;
        linesUpRef.current = was; // the pane is probably still scrolled — don't hide the exit
        inModeRef.current = wasInMode;
        setLinesUp(was);
        setInMode(wasInMode);
      })
      .finally(() => {
        if (era !== eraRef.current) return;
        exitingRef.current = false;
        scrollingRef.current = false;
        flushScroll();
      });
  };

  // gesture travel → lines. positive = toward newer output (wheel down / finger up).
  const scrollByGesture = (deltaPx: number) => {
    // Reversing drops banked travel and any queued lines going the other way, so
    // changing direction responds to THIS gesture instead of finishing the last one.
    if (Math.sign(deltaPx) !== Math.sign(gestureRef.current)) gestureRef.current = 0;
    if (Math.sign(deltaPx) !== Math.sign(pendingRef.current)) pendingRef.current = 0;
    gestureRef.current += deltaPx;
    const lines = Math.trunc(gestureRef.current / GESTURE_LINE_PX);
    if (!lines) return;
    gestureRef.current -= lines * GESTURE_LINE_PX; // keep the sub-line remainder
    // Already live, nothing below us. Each regime keeps its own depth — tmux's copy-mode
    // position, or the pages we've sent a full-screen app — and both read zero exactly at
    // the live tail, so this stays armed in either one. It has to: line 521 says the wheel
    // must never reach the app as a keystroke, and a down at claude's tail would otherwise
    // send it a PageDown, which is the most common gesture there.
    if (lines > 0 && !linesUpRef.current && !altPagesRef.current) return;
    pendingRef.current += lines;
    flushScroll();
  };

  // A pane left in copy-mode looks frozen the next time it's opened (keystrokes go to
  // copy-mode, not claude, and a fresh view starts with the badge at zero — no visible
  // way out), so always come back to live when this view goes away. In flight or merely
  // queued counts as scrolled: a request that lands after teardown would enter copy-mode
  // behind our back.
  useEffect(() => {
    linesUpRef.current = 0;
    inModeRef.current = false;
    gestureRef.current = 0;
    pendingRef.current = 0;
    scrollingRef.current = false;
    exitingRef.current = false;
    wantLiveRef.current = false;
    paneAltRef.current = null; // unknown until the first scroll response for this session
    altPagesRef.current = 0;
    setLinesUp(0);
    setInMode(false);
    const mine = sid;
    return () => {
      const dirty = scrolledBack() || scrollingRef.current || pendingRef.current;
      eraRef.current += 1; // any reply still in flight now describes a pane we've left
      if (dirty && mine) scrollTerminal(mine, "exit").catch(() => {});
    };
  }, [url, sid]);

  // Theme switch → respawn this viewer. ttyd bakes its xterm palette in at spawn
  // (`-t theme=`), and the terminal is a cross-document iframe, so it can't follow the
  // page's CSS variables the way everything else does. Reusing onDead means all four
  // hosts (card drawer, Terminals tab, orchestrator, teams) inherit this for free —
  // they already know how to re-open a viewer. respawnedRef is cleared first: that
  // guard exists for the dead-ttyd case, and a theme change is a fresh, legitimate
  // reason to respawn even if this viewer already auto-respawned once.
  useEffect(() => {
    if (!onDead) return;
    const onThemeChange = () => {
      respawnedRef.current = false;
      onDead();
    };
    window.addEventListener(THEME_EVENT, onThemeChange);
    return () => window.removeEventListener(THEME_EVENT, onThemeChange);
  }, [onDead]);

  // Reveal the terminal after `delay` (replacing any pending reveal). Kept hidden until
  // then so ttyd's load flash + xterm's fit-resize jump happen off-screen.
  const reveal = (delay: number) => {
    if (revealTimer.current) clearTimeout(revealTimer.current);
    revealTimer.current = setTimeout(() => setReady(true), delay);
  };

  // new session (url change) → hide until it loads; onIframeLoad reveals with a short
  // settle delay, this fallback guarantees it never stays hidden if onLoad is slow.
  // useLayoutEffect (pre-paint) so a session SWITCH — where the iframe is reused, not
  // remounted — never flashes the new blank frame before the hide applies.
  useLayoutEffect(() => {
    setReady(false);
    // Long, because this is only a LAST-RESORT unhide. onIframeLoad runs its own
    // poll-for-xterm with a ~600ms cap of its own, so the terminal still appears
    // promptly once the frame loads; at 600ms this timer was firing BEFORE the iframe
    // had loaded on a slow first paint and revealing an unpainted frame — the white
    // flash. Nothing normal depends on it.
    reveal(2500);
    return () => {
      if (revealTimer.current) clearTimeout(revealTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);
  // auto-reconnect backoff bookkeeping (refs: mutated by the 1s watchdog, read by the
  // manual-reconnect button — kept off React state so the watchdog doesn't re-render)
  const attemptsRef = useRef(0);
  const nextTryAtRef = useRef(0);
  const gaveUpRef = useRef(false);

  // read the (viewer-machine) clipboard image and hand it to the session's host, so
  // a Roam-clipboard screenshot reaches a Base claude despite only sharing a tailnet.
  const pasteImage = async () => {
    if (!sid || pasting) return;
    setPasting(true);
    setPasteMsg(null);
    try {
      const items = await navigator.clipboard.read();
      let blob: Blob | null = null;
      let mime = "image/png";
      for (const it of items) {
        const t = it.types.find((x) => x.startsWith("image/"));
        if (t) {
          blob = await it.getType(t);
          mime = t;
          break;
        }
      }
      if (!blob) {
        setPasteMsg({ ok: false, text: "no image on the clipboard" });
        return;
      }
      const dataUrl: string = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(r.result as string);
        r.onerror = () => rej(r.error);
        r.readAsDataURL(blob!);
      });
      await pasteTerminalImage(sid, dataUrl, mime);
      setPasteMsg({ ok: true, text: "✓ path inserted — add context, then press Enter" });
      iframeRef.current?.focus();
    } catch (e: any) {
      // clipboard read blocked (permission / not https) or the upload failed
      setPasteMsg({ ok: false, text: `⚠ ${e?.message || "paste failed"}` });
    } finally {
      setPasting(false);
      setTimeout(() => setPasteMsg(null), 6000);
    }
  };

  // iOS soft keyboard doesn't shrink the layout viewport — a maximized terminal
  // would put claude's input row UNDER the keyboard (§3F.3). Track the visual
  // viewport and cap the maximized container to it so the input stays visible.
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv || !maximized) {
      setVvh(null);
      return;
    }
    const sync = () => setVvh(vv.height < window.innerHeight - 80 ? vv.height : null);
    sync();
    vv.addEventListener("resize", sync);
    vv.addEventListener("scroll", sync);
    return () => {
      vv.removeEventListener("resize", sync);
      vv.removeEventListener("scroll", sync);
    };
  }, [maximized]);

  // ttyd's xterm doesn't always reflow when the iframe jumps size (maximize);
  // nudge it once the new layout settles (same-origin proxy → reachable).
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        iframeRef.current?.contentWindow?.dispatchEvent(new Event("resize"));
      } catch {
        /* ignore */
      }
    }, 250);
    return () => clearTimeout(t);
  }, [maximized, url]);

  // ── auto-reconnect with backoff ──────────────────────────────────────────────
  // ttyd drops its WebSocket on any blip (laptop sleep/wake, network flap, tailscale
  // re-handshake) and shows a persistent "Press ⏎ to Reconnect" overlay, waiting for a
  // human keypress. ttyd's OWN auto-reconnect is a delay-free infinite loop (it would
  // hammer a genuinely-dead backend), so we leave it off and drive reconnection here.
  // Same-origin proxy (/term/<id>/) lets us read ttyd's overlay (a bare, non-xterm div on
  // window.term.element) and send Enter into xterm's helper textarea — exactly the human
  // gesture. Exponential backoff paces the retries; after a cap we stop and surface a
  // manual button instead of pounding a dead server. (Coupled to ttyd 1.7.x internals:
  // window.term + the "Press ⏎ to Reconnect" overlay; if ttyd changes, this degrades to
  // today's manual behaviour.)
  useEffect(() => {
    const BASE = 1000;
    const CAP = 30_000;
    const MAX_ATTEMPTS = 8; // ~1+2+4+8+16+30+30s of trying before giving up
    const ENTER_TRIES = 3; // send Enter first (smooth); escalate to iframe reload after
    attemptsRef.current = 0;
    nextTryAtRef.current = 0;
    gaveUpRef.current = false;

    type TW = (Window & { term?: { element?: HTMLElement } }) | null;
    // ttyd's overlayNode is the one child of term.element with no `xterm-*` class
    const overlayNode = (w: TW): HTMLElement | null => {
      const el = w?.term?.element;
      if (!el) return null;
      for (const c of Array.from(el.children) as HTMLElement[]) {
        if (!String(c.className || "").includes("xterm") && (c.textContent || "").length) return c;
      }
      return null;
    };
    const readState = (w: TW): "connected" | "reconnect" | "reconnecting" | "unknown" => {
      if (!w?.term?.element) return "unknown"; // iframe still loading / navigated away
      const ov = overlayNode(w);
      if (!ov || !ov.parentNode || ov.style.opacity === "0") return "connected";
      // "Press ⏎ to Reconnect" = actionable; "Reconnecting…"/"Connection Closed" = in flight
      return /Press.*Reconnect/i.test(ov.textContent || "") ? "reconnect" : "reconnecting";
    };
    const sendEnter = (w: TW) => {
      const ta = w?.term?.element?.querySelector(".xterm-helper-textarea");
      if (!ta) return;
      const KE = (w as (Window & { KeyboardEvent: typeof KeyboardEvent }) | null)?.KeyboardEvent || KeyboardEvent;
      // xterm's onKey fires ttyd's "Enter → reconnect" handler; keydown alone suffices
      ta.dispatchEvent(
        new KE("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }),
      );
    };
    const reload = () => {
      const f = iframeRef.current;
      if (!f) return;
      setReady(false); // hide during the reload; onIframeLoad fades it back in
      reveal(600);
      try {
        f.contentWindow?.location.reload();
      } catch {
        try {
          f.src = f.src;
        } catch {
          /* ignore */
        }
      }
    };

    const tick = () => {
      let w: TW = null;
      try {
        w = iframeRef.current?.contentWindow as TW;
      } catch {
        w = null; // cross-origin blip mid-navigation
      }
      const s = readState(w);
      if (s === "unknown" || s === "reconnecting") return; // loading, or an attempt in flight
      if (s === "connected") {
        if (attemptsRef.current || gaveUpRef.current) {
          attemptsRef.current = 0;
          nextTryAtRef.current = 0;
          gaveUpRef.current = false;
          setLinkState("ok");
        }
        return;
      }
      // s === "reconnect": ttyd is parked on "Press ⏎ to Reconnect"
      if (gaveUpRef.current) return;
      const now = Date.now();
      if (nextTryAtRef.current && now < nextTryAtRef.current) return; // still in the backoff window
      if (attemptsRef.current >= MAX_ATTEMPTS) {
        gaveUpRef.current = true;
        setLinkState("lost");
        return;
      }
      attemptsRef.current += 1;
      if (attemptsRef.current <= ENTER_TRIES) sendEnter(w);
      else reload();
      nextTryAtRef.current = now + Math.min(BASE * 2 ** (attemptsRef.current - 1), CAP);
      setLinkState("retrying");
    };

    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [url]);

  // manual "give up → try again": reset backoff and force a fresh connect
  const reconnectNow = () => {
    attemptsRef.current = 0;
    nextTryAtRef.current = 0;
    gaveUpRef.current = false;
    setLinkState("retrying");
    setReady(false);
    reveal(600);
    const f = iframeRef.current;
    try {
      f?.contentWindow?.location.reload();
    } catch {
      try {
        if (f) f.src = f.src;
      } catch {
        /* ignore */
      }
    }
  };

  const zoom = (d: number) => onZoom?.(Math.max(8, Math.min(28, fontSize + d)));

  // Wires the listeners we can only attach because the terminal is same-origin
  // (/term/… proxy): reveal gating, scroll paging, soft-keyboard suppression and
  // the selection remap all live on the iframe's OWN document.
  const onIframeLoad = () => {
    // Reveal as soon as xterm has actually opened — the fit + first paint (the "jump")
    // happen right after that, so waiting for it is enough. Poll for it (fast when the
    // ttyd token fetch is quick) instead of a blind fixed delay, capped so a stuck load
    // still shows. First thing in the handler so an early return / listener throw below
    // can't skip it and strand the terminal on the long fallback.
    let tries = 0;
    const armReveal = () => {
      const tw = iframeRef.current?.contentWindow as (Window & { term?: { element?: HTMLElement } }) | null;
      if (tw?.term?.element) {
        respawnedRef.current = false; // a healthy xterm clears the auto-respawn guard
        return reveal(40); // xterm up → a couple frames for fit, then fade
      }
      if (tries++ >= 20) {
        // term never came up — is this the proxy's "terminal ended" page (a dead ttyd
        // after a backend restart)? tmux+claude usually outlive the ttyd, so auto-respawn
        // ONCE (a fresh viewer re-attaches) instead of stranding the manual close/reopen.
        let dead = false;
        try {
          dead = !!tw?.document?.body?.textContent?.includes("terminal ended");
        } catch {
          /* cross-origin — won't happen for the same-origin proxy */
        }
        if (onDead && !respawnedRef.current && dead) {
          respawnedRef.current = true;
          onDead();
          return;
        }
        return reveal(0); // ~600ms cap: show regardless
      }
      clearTimeout(revealTimer.current);
      revealTimer.current = setTimeout(armReveal, 30);
    };
    armReveal();

    const w = iframeRef.current?.contentWindow as (Window & typeof globalThis) | null;
    if (!w) return;
    const doc = w.document;
    // stop the browser's pull-to-refresh / overscroll inside the terminal
    try {
      doc.documentElement.style.overscrollBehavior = "none";
      if (doc.body) doc.body.style.overscrollBehavior = "none";
    } catch {
      /* ignore */
    }
    // mobile: on a phone you type via the on-screen input bar (TermKeys), never
    // xterm's hidden textarea directly. xterm auto-focuses that textarea on load,
    // popping the soft keyboard unprompted. inputmode=none lets xterm keep focus
    // (soft-keys still dispatch onto it) WITHOUT the keyboard showing. Retried a
    // few times because xterm creates/refocuses the textarea during its own init.
    if (w.matchMedia("(pointer: coarse)").matches) {
      const noKb = () => {
        const ta = doc.querySelector(".xterm-helper-textarea") as HTMLTextAreaElement | null;
        if (ta) ta.setAttribute("inputmode", "none");
        (doc.activeElement as HTMLElement | null)?.blur?.();
      };
      noKb();
      w.setTimeout(noKb, 150);
      w.setTimeout(noKb, 500);
    }
    // The wheel: let it through, or route it server-side.
    //
    // NATIVE (default) — do nothing, and the event takes the path a plain terminal
    // gives it: xterm encodes a mouse report, ttyd ships it to tmux, and tmux hands it
    // to the app (`mouse on` + the app's own mouse reporting — every claude pane runs
    // with `#{mouse_any_flag}` 1). claude then scrolls its OWN transcript, line by
    // line, with no round trip. That is why scrolling a claude pane in a plain tmux is
    // smooth while routing each gesture through HTTP is not: one wheel notch here cost
    // a request, a tmux exec and a pane redraw.
    // The old fear behind swallowing it — that the wheel arrives as ↑/↓ and claude
    // reads them as prompt history — is tmux's alternate-scroll translation, which
    // applies only to an app that has NOT asked for mouse reporting. claude has.
    //
    // SERVER (localStorage conductor.wheelServerSide = true) — the previous behaviour,
    // kept because the native path depends on the app honouring mouse reporting: a TUI
    // that doesn't would otherwise be unscrollable, and this is the way back without a
    // rebuild. The touch path below always routes server-side regardless: a finger drag
    // produces no wheel event for xterm to encode.
    if (WHEEL_SERVER_SIDE) {
      doc.addEventListener(
        "wheel",
        (e: WheelEvent) => {
          e.preventDefault();
          e.stopImmediatePropagation();
          scrollByGesture(
            e.deltaMode === 1 ? e.deltaY * WHEEL_LINE_PX : e.deltaMode === 2 ? e.deltaY * WHEEL_PAGE_PX : e.deltaY,
          );
        },
        { capture: true, passive: false },
      );
    }
    // Typing while scrolled back: tmux would read the input as a copy-mode command, so
    // spend it on returning to the live prompt instead of letting it act. Both input
    // paths need this — TermKeys types on mobile by dispatching synthetic `input`
    // events on xterm's textarea (the only way a third-party IME reaches xterm), which
    // no keydown listener ever sees. (Clicks are left alone: drag-selecting scrollback
    // text is the point of being up here.)
    for (const kind of ["keydown", "beforeinput", "input"]) {
      doc.addEventListener(
        kind,
        (e: Event) => {
          if (!scrolledBack()) return;
          e.preventDefault();
          e.stopImmediatePropagation();
          toLive();
        },
        { capture: true },
      );
    }

    let lastY: number | null = null;
    doc.addEventListener(
      "touchstart",
      (e: TouchEvent) => {
        lastY = e.touches[0]?.clientY ?? null;
      },
      { passive: true },
    );
    doc.addEventListener(
      "touchmove",
      (e: TouchEvent) => {
        if (lastY == null) return;
        e.preventDefault(); // suppress native scroll + pull-to-refresh; we page instead
        const y = e.touches[0]?.clientY ?? lastY;
        scrollByGesture(lastY - y); // finger up → positive → toward newer output
        lastY = y;
      },
      { passive: false },
    );
    doc.addEventListener("touchend", () => {
      lastY = null;
    });

    // ── key remap ────────────────────────────────────────────────────────────
    // Capture phase so we beat xterm's own keydown handler, and preventDefault
    // so Chrome doesn't take ⌘←/⌘⌫ as history-back. Table (+ its optional
    // ~/.conductor/hotkeys.json override) lives in TermKeys.
    loadHotkeys();
    doc.addEventListener(
      "keydown",
      (e: KeyboardEvent) => {
        if (!e.isTrusted) return; // TermKeys' synthetic soft-key events pass through
        // Mac Chinese IMEs use CapsLock as the 中/英 toggle, which commits the
        // in-flight composition TWICE: once via compositionend, and once via xterm's
        // CompositionHelper.keydown, whose "any key but Shift/Ctrl/Alt (16/17/18)
        // ends composition" branch doesn't exempt CapsLock (20). Swallowing the
        // keydown while composing kills the duplicate; compositionend still commits.
        if (e.code === "CapsLock" && (e.isComposing || doc.querySelector(".composition-view.active"))) {
          e.stopImmediatePropagation();
          return;
        }
        // Never steal a keystroke the IME is still composing with — S-Enter is the
        // first Shift-only binding here, and swallowing it mid-composition would put
        // the LF on the wire ahead of the Chinese, which only lands on compositionend.
        if (e.isComposing) return;
        const seq = hotkeySeq(e);
        if (!seq) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        injectText(iframeRef.current, seq, { raw: true });
      },
      { capture: true },
    );

    // ── selection & copy ─────────────────────────────────────────────────────
    // claude keeps mouse tracking ON, so plain drag goes to the app. xterm.js's
    // "force local selection" modifier is Shift on Win/Linux but ⌥ Option on Mac
    // (shouldForceSelection: isMac ? altKey && macOptionClickForcesSelection :
    // shiftKey) — so Shift+drag on a Mac selected nothing, which is why copying
    // failed. Remap: a Shift+drag mousedown is re-dispatched as an Option one
    // (xterm ignores isTrusted), so the familiar Shift gesture selects locally.
    doc.addEventListener(
      "mousedown",
      (e: MouseEvent) => {
        if (!e.isTrusted || !e.shiftKey || e.altKey || e.button !== 0) return;
        e.stopImmediatePropagation();
        e.preventDefault();
        try {
          e.target?.dispatchEvent(
            new MouseEvent("mousedown", {
              bubbles: true,
              cancelable: true,
              composed: true,
              view: w,
              detail: e.detail,
              button: e.button,
              buttons: e.buttons,
              clientX: e.clientX,
              clientY: e.clientY,
              screenX: e.screenX,
              screenY: e.screenY,
              altKey: true, // ← the actual remap
              ctrlKey: e.ctrlKey,
              metaKey: e.metaKey,
            }),
          );
        } catch {
          /* fall back to the app-side drag */
        }
      },
      { capture: true },
    );
    // copy-on-select: when the drag ends with a selection, write it straight to
    // the clipboard (mouseup = user gesture, so navigator.clipboard is allowed).
    doc.addEventListener("mouseup", () => {
      setTimeout(() => {
        try {
          const term = (w as unknown as { term?: { hasSelection(): boolean; getSelection(): string } }).term;
          if (!term?.hasSelection()) return;
          const text = term.getSelection();
          if (!text.trim()) return;
          navigator.clipboard?.writeText(text).then(() => {
            setCopied(true);
            if (copiedTimer.current) clearTimeout(copiedTimer.current);
            copiedTimer.current = setTimeout(() => setCopied(false), 1500);
          }).catch(() => {});
        } catch {
          /* ignore */
        }
      }, 50); // let xterm finalize the selection first
    });

  };

  return (
    <div
      style={maximized && vvh ? { height: vvh } : undefined}
      // data-term-maximized: hook for ancestors that form their own stacking
      // context (CardDetail's z-50 drawer) to raise themselves above the app
      // header (z-[55]) while a terminal inside them is fullscreen — the inner
      // z-[60] only competes within that ancestor's context, not with the header.
      data-term-maximized={maximized ? "" : undefined}
      className={
        maximized
          ? "fixed inset-x-0 top-0 z-[60] bg-black p-2 pb-[env(safe-area-inset-bottom)] flex flex-col gap-1 h-[100dvh]"
          : fill
            ? "flex flex-col gap-1 h-full min-h-0"
            : "flex flex-col gap-1"
      }
    >
      {(!hideHeader || maximized) && (
      <div ref={toolbarRef} className="flex items-center gap-2 text-[11px] flex-wrap">
        <button
          onClick={() => setMaximized((m) => !m)}
          className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          title={maximized ? "exit fullscreen" : "maximize"}
        >
          {maximized ? (narrow ? "⤡" : "⤡ exit") : narrow ? "⤢" : "⤢ maximize"}
        </button>
        <button
          onClick={async () => {
            if (!sid) return;
            try {
              const { text } = await captureTerminal(sid);
              setCopyText(text);
            } catch {
              /* ignore */
            }
          }}
          className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          title="copy text from the terminal (tip: Shift/⌥+drag-select auto-copies)"
        >
          {narrow ? "📋" : "📋 copy"}
        </button>
        {copied && <span className="text-emerald-400">✓ copied</span>}
        <button
          onClick={pasteImage}
          disabled={pasting}
          className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 disabled:opacity-50"
          title="paste a clipboard image to Claude — saved on the session's machine, path inserted; add context and press Enter"
        >
          {pasting ? "…" : narrow ? "📎" : "📎 paste image"}
        </button>
        {pasteMsg && (
          <span className={pasteMsg.ok ? "text-emerald-400" : "text-red-400"}>{pasteMsg.text}</span>
        )}
        {onZoom && (
          <>
            <button
              onClick={() => zoom(-2)}
              className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
              title="smaller font"
            >
              A−
            </button>
            {!narrow && <span className="text-zinc-500">{fontSize}px</span>}
            <button
              onClick={() => zoom(2)}
              className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
              title="larger font"
            >
              A+
            </button>
          </>
        )}
        <div className="relative">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
            title="more — new tab / hand off"
          >
            ⋯
          </button>
          {menuOpen && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setMenuOpen(false)} />
              <div className="absolute right-0 z-50 mt-1 min-w-[150px] flex flex-col gap-0.5 p-1 rounded bg-zinc-900 border border-zinc-700 shadow-xl">
                {narrow && (
                  /* narrow row: the font-size label + host chip live here */
                  <div className="px-2 py-1 flex items-center gap-1.5 text-zinc-400">
                    <span>字級 {fontSize}px</span>
                    {headerExtra}
                  </div>
                )}
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => setMenuOpen(false)}
                  className="px-2 py-1 rounded text-left text-zinc-300 hover:bg-zinc-800"
                >
                  new tab ↗
                </a>
                {menuExtra}
                {/* plugin menu slot — every plugin's card_widget slot="menu" row,
                    rendered by the component that OWNS the ⋯ menu so it appears
                    identically at every terminal surface */}
                {pluginMenuRows.map((w) => {
                  const L = CARD_LAYOUTS[w.layout];
                  return L ? <L key={w.id} data={w.data} /> : null;
                })}
              </div>
            </>
          )}
        </div>
        {linkState === "retrying" && (
          <span className="text-amber-400 animate-pulse" title="connection dropped — auto-reconnecting (with backoff)">
            ⟳ reconnecting…
          </span>
        )}
        {linkState === "lost" && (
          <button
            onClick={reconnectNow}
            className="px-2 py-0.5 rounded bg-amber-600/80 hover:bg-amber-600 text-white border border-amber-500"
            title="auto-reconnect gave up (may be truly unreachable) — click to retry"
          >
            ⚠ connection lost · reconnect
          </button>
        )}
        {!narrow && headerExtra}
        {(inMode || linesUp > 0) && (
          <button
            onClick={toLive}
            className="px-2 py-0.5 rounded bg-sky-600/80 hover:bg-sky-600 text-white border border-sky-500"
            title="scrolled back through tmux's scrollback — typing is paused until you return to the live prompt"
          >
            ⇡ {linesUp > 0 ? `${linesUp} lines back` : "scrolled back"} · to live
          </button>
        )}
        {(onKill || onClose) && (
          <div className="ml-auto flex items-center gap-1.5">
            {onKill && (
              <button
                onClick={onKill}
                className="px-2 py-0.5 rounded bg-red-600/80 hover:bg-red-600 text-white border border-red-500"
                title="terminate claude + kill the tmux session"
              >
                {narrow ? "✖" : "✖ Kill claude"}
              </button>
            )}
            {onClose && (
              <button
                onClick={onClose}
                className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-300"
                title="close this view — leaves claude running in tmux"
              >
                {narrow ? "✕" : "Close"}
              </button>
            )}
          </div>
        )}
      </div>
      )}
      <div
        className={
          maximized
            ? `relative flex-1 w-full overflow-hidden rounded bg-black border-2 ${hostFrameClass(host)}`
            : fill
              ? `relative flex-1 w-full min-h-0 overflow-hidden rounded bg-black border ${hostFrameClass(host)}`
              : "relative w-full rounded bg-black"
        }
      >
        <iframe
          ref={iframeRef}
          src={url}
          onLoad={onIframeLoad}
          allow="clipboard-write; clipboard-read"
          title="terminal"
          className={`bg-surface-base transition-opacity duration-150 ${ready ? "opacity-100" : "opacity-0"} ${
            maximized || fill
              ? "absolute inset-0 h-full w-full" // fill the wrapper (no drag-resize in these modes)
              : // card drawer: keep resize-y ON the iframe so its native drag handle works
                // sm heights are keysOpen-aware too: on a TABLET the keys bar now shows
                // (TermKeys' coarse-pointer gate), and a fixed sm:h-[38rem] would let the
                // bar ADD height below the drawer instead of the terminal yielding to it.
                `w-full ${keysOpen ? "h-[56vh] sm:h-[30rem]" : "h-[80vh] sm:h-[38rem]"} min-h-[240px] resize-y overflow-auto rounded border ${hostFrameClass(host)}`
          }`}
        />
        {!ready && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center rounded bg-surface-base">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-zinc-600 border-t-transparent" />
          </div>
        )}
      </div>
      <TermKeys
        getIframe={() => iframeRef.current}
        collapsed={!keysOpen}
        onToggle={() => setKeysOpen((k) => !k)}
      />
      {copyText !== null && (
        <div
          className="fixed inset-0 z-[70] bg-black/60 flex items-center justify-center p-3"
          onClick={() => setCopyText(null)}
        >
          <div
            className="w-full max-w-2xl bg-zinc-900 border border-zinc-700 rounded-xl p-3 space-y-2"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 text-xs">
              <span className="text-zinc-300">select text to copy, or press "Copy all"</span>
              <button
                onClick={() => navigator.clipboard?.writeText(copyText).catch(() => {})}
                className="ml-auto px-2.5 py-1 rounded bg-sky-600 hover:bg-sky-500 text-white"
              >
                Copy all
              </button>
              <button
                onClick={() => setCopyText(null)}
                className="px-2.5 py-1 rounded bg-zinc-800 border border-zinc-700 text-zinc-300"
              >
                Close
              </button>
            </div>
            <textarea
              readOnly
              value={copyText}
              // 16px for the same iOS reason as every other field here. This one is
              // readOnly — Safari still focuses it on tap, and only `disabled` is
              // reliably exempt — so it gets the threshold too rather than relying on
              // readOnly being treated as an exemption.
              className="w-full h-[60vh] text-base font-mono px-2 py-1.5 rounded bg-zinc-950 border border-zinc-800 text-zinc-200"
            />
          </div>
        </div>
      )}
    </div>
  );
}
