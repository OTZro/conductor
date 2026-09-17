// Mobile soft-key bar + live text input + scroll for the web terminal.
// Phone keyboards lack Esc/Ctrl/arrows/Tab (tmux + claude TUI need them), and
// third-party IME keyboards (e.g. iAccess) don't compose into ttyd's xterm. So:
//   - control keys → synthetic keydown into xterm's helper textarea
//   - text typed in a normal <input> (any keyboard works there) is injected LIVE
//     into the terminal via input events — Chinese commits on selection. This is
//     the only reliable way to use a third-party IME, since xterm won't capture it.
//   - scroll → tmux copy-mode paging, owned by TerminalView (see its pager)
//   - tmux tabs → raw Option+key escape bytes
// Same-origin iframe (/term/…) → contentWindow is reachable. Best-effort.
//
// The key buttons are config-driven: an optional ~/.conductor/termkeys.json overrides
// the built-in DEFAULT_KEYS (served via /api/termkeys). Each key either injects a raw
// byte `seq` (tmux-style, green) or fires a synthetic keydown (key/keyCode, zinc).
import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useRef, useState } from "react";
import { getHotkeys, getTermKeys, type TTermKey } from "../api";

// WHICH DEVICES GET THIS BAR — capability, not width. TerminalView suppresses the soft
// keyboard on any COARSE-POINTER device (it sets inputmode="none" on xterm's helper
// textarea — see its `(pointer: coarse)` branch) precisely because typing is meant to
// happen HERE instead. A width-only gate (`sm:hidden`, <640px) disagreed with that on a
// tablet: an iPad is coarse-pointer AND ≥640, so it got the soft keyboard suppressed AND
// this bar hidden — no way to type at all. Matching the SAME predicate keeps the two in
// step on any touch device, both orientations. The `sm:hidden` fallback stays for
// fine-pointer devices, so a narrow desktop window behaves exactly as before.
const IS_COARSE =
  typeof window !== "undefined" && !!window.matchMedia?.("(pointer: coarse)").matches;

// keyCode is REQUIRED: xterm's keydown handler dispatches on `switch(e.keyCode)`
// (case 27 = Esc, 38 = ↑, …). A synthetic KeyboardEvent ignores keyCode in its
// init dict (stays 0), so we also patch it on in sendKey — without it every
// arrow/Tab/Esc mapped to nothing and the buttons looked dead.
const DEFAULT_KEYS: TTermKey[] = [
  { label: "Esc", key: "Escape", code: "Escape", keyCode: 27 },
  { label: "Tab", key: "Tab", code: "Tab", keyCode: 9 },
  { label: "↵", key: "Enter", code: "Enter", keyCode: 13 },
  { label: "↑", key: "ArrowUp", code: "ArrowUp", keyCode: 38 },
  { label: "↓", key: "ArrowDown", code: "ArrowDown", keyCode: 40 },
  { label: "←", key: "ArrowLeft", code: "ArrowLeft", keyCode: 37 },
  { label: "→", key: "ArrowRight", code: "ArrowRight", keyCode: 39 },
  { label: "Home", key: "Home", code: "Home", keyCode: 36 },
  { label: "End", key: "End", code: "End", keyCode: 35 },
  { label: "^C", key: "c", code: "KeyC", ctrlKey: true, keyCode: 67 },
  { label: "^D", key: "d", code: "KeyD", ctrlKey: true, keyCode: 68 },
  { label: "^R", key: "r", code: "KeyR", ctrlKey: true, keyCode: 82 },
  // tmux (⌥ = Meta): prev/next window, ⌥t/⌥w, jump ⌥1-9 — raw Option+key escape bytes
  { label: "⌥←", seq: "\x1b[1;3D", title: "prev window (⌥←)" },
  { label: "⌥→", seq: "\x1b[1;3C", title: "next window (⌥→)" },
  { label: "⌥t", seq: "\x1bt", title: "⌥t" },
  { label: "⌥w", seq: "\x1bw", title: "⌥w" },
  ...Array.from({ length: 9 }, (_, i) => ({
    label: `⌥${i + 1}`,
    seq: `\x1b${i + 1}`,
    title: `window ${i + 1} (⌥${i + 1})`,
  })),
];

function termTextarea(iframe: HTMLIFrameElement | null): HTMLTextAreaElement | null {
  const w = iframe?.contentWindow as (Window & typeof globalThis) | null;
  if (!w) return null;
  return w.document.querySelector(".xterm-helper-textarea") as HTMLTextAreaElement | null;
}

function sendKey(iframe: HTMLIFrameElement | null, init: KeyboardEventInit & { keyCode?: number }) {
  const ta = termTextarea(iframe);
  const w = iframe?.contentWindow as (Window & typeof globalThis) | null;
  if (!ta || !w) return;
  ta.focus();
  const ev = new w.KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  // KeyboardEvent.keyCode/which are read-only accessors the constructor won't set
  // from the init dict — shadow them on the instance so xterm's switch(e.keyCode)
  // sees the real value (arrows/Tab/Esc otherwise map to keyCode 0 = nothing).
  if (init.keyCode != null) {
    Object.defineProperty(ev, "keyCode", { get: () => init.keyCode });
    Object.defineProperty(ev, "which", { get: () => init.keyCode });
  }
  ta.dispatchEvent(ev);
}

// ── desktop physical-keyboard remap ────────────────────────────────────────
// xterm.js already encodes Ctrl/⌥ + arrows (`\x1b[1;6D` …), so tmux's C-S-←/→
// work as-is. It sends NOTHING for two families, and sends the WRONG byte for a
// third — all three are what this table fixes:
//   ⌘ + key      — the browser owns Cmd; xterm.js ignores metaKey entirely
//   Ctrl+Shift+letter — needs CSI u / modifyOtherKeys, which xterm.js lacks
//   Shift+Enter  — xterm.js sends plain CR, indistinguishable from Enter
// ⌥ + arrows/delete are here too so ⌥ + LETTER still types ∫ƒ∂ (which the
// ttyd-level `macOptionIsMeta` flag would have killed). Bytes mirror iTerm2's
// Natural Text Editing map; the two Ctrl+Shift ones mirror tmux.conf's bindings.
// Override the whole table per-machine with ~/.conductor/hotkeys.json (see
// api/termkeys.py) — same escape hatch as termkeys.json, no repo edit needed.
// NOTE: a non-empty hotkeys.json REPLACES this table wholesale (it does not merge),
// so an existing override has to re-add any key added here — S-Enter included.
const DEFAULT_HOTKEYS: Record<string, string> = {
  // Shift+Enter → newline, not submit. xterm sends CR (\r) for BOTH Enter and
  // Shift+Enter, so claude's input takes Shift+Enter as submit. claude recognises LF
  // (\n, 0x0A) as "insert a newline" (the byte iTerm2 sends for Shift+Enter), and tmux
  // passes the raw byte straight through — so remap it here. Both Enters: the table
  // keys on e.code, and an external keyboard's numpad reports NumpadEnter.
  "S-Enter": "\n",
  "S-NumpadEnter": "\n",
  "M-ArrowLeft": "\x01", // ⌘← line start (^A)
  "M-ArrowRight": "\x05", // ⌘→ line end (^E)
  "M-Backspace": "\x15", // ⌘⌫ kill line (^U)
  "A-ArrowLeft": "\x1bb", // ⌥← word left
  "A-ArrowRight": "\x1bf", // ⌥→ word right
  "A-Backspace": "\x1b\x7f", // ⌥⌫ delete word back
  "A-Delete": "\x1bd", // ⌥⌦ delete word forward
  "CS-KeyV": "\x1b[27;6;118~", // tmux C-S-v → paste image from @ccimg-host
  "CS-KeyU": "\x1b[27;6;117~", // tmux C-S-u → URL picker popup
};

// Held in a module-level ref, not React state: the keydown listener lives on the
// ttyd iframe's document and must resolve a key synchronously, outside any render.
let hotkeys: Record<string, string> = DEFAULT_HOTKEYS;

// Load the optional local override once. A non-empty file REPLACES the defaults
// (same semantics as termkeys.json), which is what lets it DELETE a shipped key —
// the cost being that a key added to DEFAULT_HOTKEYS never reaches anyone who
// already wrote the file. Empty/failed → keep the built-ins.
export function loadHotkeys() {
  getHotkeys()
    .then((cfg) => {
      if (Object.keys(cfg).length) hotkeys = cfg;
    })
    .catch(() => {});
}

// keyed on e.code, not e.key: ⌥/⇧ mutate e.key ("v"→"V", "b"→"∫") but never e.code.
export function hotkeySeq(e: KeyboardEvent): string | undefined {
  const mods = `${e.metaKey ? "M" : ""}${e.ctrlKey ? "C" : ""}${e.altKey ? "A" : ""}${e.shiftKey ? "S" : ""}`;
  return mods ? hotkeys[`${mods}-${e.code}`] : undefined;
}

// `raw` picks the transport by INTENT, not by what the bytes happen to contain:
// a hotkey/soft-key sequence is a literal byte string the pty must receive verbatim,
// so it goes through xterm's data channel (`Terminal.input`, public API → onData →
// ttyd's websocket) rather than the helper textarea. Typed text keeps the textarea
// path, which is what makes third-party IMEs work (see the file header).
//
// The textarea path is not a safe fallback for a raw sequence. It reaches the pty via
// xterm's `_inputEvent`, which drops the event outright when `_keyPressHandled` is
// still set from the previous keystroke — and our capture-phase handler in
// TerminalView preventDefaults the hotkey, so xterm's own keydown never runs to reset
// that flag. (It does NOT rewrite \n → \r, as an earlier version of this comment
// claimed: `_inputEvent` passes `ev.data` to `triggerDataEvent` unchanged, and the
// \r?\n → \r normalization lives only in `prepareTextForTerminal`, on the paste path.)
// So when the data channel is missing — ttyd assigns `window.term` inside its `open()`,
// so the load window right after the iframe mounts has no `term` yet — fail closed.
// Falling through would submit the user's half-written prompt, the exact bug this fixes.
export function injectText(iframe: HTMLIFrameElement | null, text: string, opts?: { raw?: boolean }) {
  const w = iframe?.contentWindow as
    | (Window & typeof globalThis & {
        InputEvent: typeof InputEvent;
        term?: { input(data: string, wasUserInput?: boolean): void };
      })
    | null;
  if (!w || !text) return;
  if (opts?.raw) {
    w.term?.input(text, true);
    return;
  }
  const ta = termTextarea(iframe);
  if (!ta) return;
  ta.value = text;
  ta.dispatchEvent(new w.InputEvent("input", { bubbles: true, data: text, inputType: "insertText" }));
}

const KEYBTN =
  "px-2.5 py-1 rounded bg-zinc-800 border border-zinc-700 text-xs font-mono text-zinc-200 active:bg-zinc-600";
const TABBTN =
  "px-2.5 py-1 rounded bg-emerald-600/25 border border-emerald-500/40 text-xs font-mono text-emerald-100 active:bg-emerald-600/50";
// the keys show/hide toggle, sitting to the right of the input so the input
// cedes just enough width for it (no separate toolbar row).
const TOGGLEBTN =
  "shrink-0 whitespace-nowrap px-3 rounded bg-zinc-700 border border-zinc-600 text-sm text-zinc-200 active:bg-zinc-600";

export function TermKeys({
  getIframe,
  collapsed = false,
  onToggle,
}: {
  getIframe: () => HTMLIFrameElement | null;
  collapsed?: boolean;
  onToggle?: () => void;
}) {
  const [text, setText] = useState("");
  const [keys, setKeys] = useState<TTermKey[]>(DEFAULT_KEYS);
  const composing = useRef(false); // IME in progress (don't inject the in-flight pinyin/zhuyin)
  const skipNext = useRef(false); // skip the onChange that trails compositionend

  // load the optional local override; empty/failed → keep the built-in defaults
  useEffect(() => {
    let stop = false;
    getTermKeys()
      .then((cfg) => {
        if (!stop && cfg.length) setKeys(cfg);
      })
      .catch(() => {});
    return () => {
      stop = true;
    };
  }, []);

  // live-inject what's typed; commit Chinese only when the IME finishes
  const flush = (v: string) => {
    if (v) injectText(getIframe(), v);
    setText("");
  };
  const onChange = (v: string) => {
    if (composing.current) {
      setText(v); // show the in-progress composition
    } else if (skipNext.current) {
      skipNext.current = false;
      setText("");
    } else {
      flush(v);
    }
  };

  const fireKey = (k: TTermKey) =>
    sendKey(getIframe(), {
      key: k.key,
      code: k.code,
      keyCode: k.keyCode ?? 0,
      ctrlKey: k.ctrlKey,
      altKey: k.altKey,
      shiftKey: k.shiftKey,
      metaKey: k.metaKey,
    });

  const down = (fn: () => void) => (e: { preventDefault: () => void }) => {
    e.preventDefault();
    fn();
  };

  return (
    <div className={`space-y-1 ${IS_COARSE ? "" : "sm:hidden"}`}>
      <div className="flex gap-1 items-stretch">
        <input
          value={text}
          onChange={(e) => onChange(e.target.value)}
          onCompositionStart={() => {
            composing.current = true;
          }}
          onCompositionEnd={(e) => {
            composing.current = false;
            flush(e.currentTarget.value); // commit the chosen Chinese
            skipNext.current = true; // its trailing onChange would re-inject
          }}
          onKeyDown={(e: ReactKeyboardEvent) => {
            if (e.nativeEvent.isComposing || composing.current) return;
            if (e.key === "Enter") {
              e.preventDefault();
              // same split as the desktop table: Shift+Enter is a newline (raw LF),
              // plain Enter submits — an iPad with an external keyboard needs both.
              if (e.shiftKey) {
                injectText(getIframe(), "\n", { raw: true });
              } else {
                injectText(getIframe(), "\r"); // send to claude / shell
                setText("");
              }
            } else if (e.key === "Backspace" && text === "") {
              e.preventDefault();
              injectText(getIframe(), "\x7f"); // delete in the terminal
            }
          }}
          placeholder="type here → streams to the terminal (IME OK, Enter to send)"
          // 16px (text-base) is the iOS no-auto-zoom threshold — anything smaller
          // makes Safari zoom the page when the input takes focus. Keep it ≥16.
          className="flex-1 min-w-0 text-base px-2 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-zinc-100"
        />
        {onToggle && (
          <button type="button" onClick={onToggle} title={collapsed ? "show keys" : "hide keys"} className={TOGGLEBTN}>
            {collapsed ? "⌨ keys" : "⌨ hide"}
          </button>
        )}
      </div>
      {!collapsed && (
        <div className="flex flex-wrap gap-1">
          {keys.map((k, i) => (
            <button
              key={`${k.label}-${i}`}
              onPointerDown={down(() => (k.seq != null ? injectText(getIframe(), k.seq!) : fireKey(k)))}
              title={k.title}
              className={k.seq != null ? TABBTN : KEYBTN}
            >
              {k.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
