import { useCallback, useEffect, useRef, useState } from "react";
import { Markdown } from "./Markdown";

// Shared in-app preview overlay for markdown/HTML files, so clicking a report doesn't
// force a download. Used by officraft (chat attachments + task artifacts) and the
// files plugin (card_widget file rows) — same proxy-href contract: same-origin,
// fetchable with the browser's own cookies, real filename honoured on download.

const MAX_PREVIEW_BYTES = 5 * 1024 * 1024; // ~5MB — past this we skip the fetch/render
// entirely and fall back to a plain download; parsing/laying out a multi-MB markdown
// tree or handing a huge srcDoc to an iframe is enough to freeze the tab.

export type PreviewKind = "markdown" | "html" | "text";

/** filename/mime → what kind of in-app preview applies, or null for "download as today". */

// JSON reads far better indented; anything that fails to parse (jsonl, trailing junk)
// just shows verbatim. Only .json files are attempted — pretty-printing a 4MB csv
// through JSON.parse would be wasted work.
function prettyText(filename: string, text: string): string {
  if (/\.json$/i.test(filename)) {
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      /* verbatim */
    }
  }
  return text;
}

export function isPreviewable(filename: string | null | undefined, mime: string | null | undefined): PreviewKind | null {
  const m = (mime || "").toLowerCase();
  const f = (filename || "").toLowerCase();
  if (m.includes("markdown") || f.endsWith(".md") || f.endsWith(".markdown")) return "markdown";
  if (m === "text/html" || f.endsWith(".html") || f.endsWith(".htm")) return "html";
  // Plain-text family: anything a person would open in an editor previews as a
  // monospace block (JSON gets pretty-printed below). Extension list over mime
  // sniffing — several sources here carry no mime at all (files plugin rows).
  if (
    m.startsWith("text/") ||
    m === "application/json" ||
    /\.(txt|log|json|jsonl|ya?ml|toml|ini|csv|tsv|xml|sh|bash|zsh|py|ts|tsx|js|jsx|go|rs|rb|java|sql|env|conf|diff|patch)$/.test(f)
  )
    return "text";
  return null;
}

type LoadState =
  | { status: "loading" }
  | { status: "ready"; text: string }
  | { status: "toolarge" }
  | { status: "error"; message: string };

export function FilePreview({
  href,
  filename,
  kind,
  onClose,
}: {
  href: string; // same-origin proxy href — the same one the download link already uses
  filename: string;
  kind: PreviewKind;
  onClose: () => void;
}) {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    (async () => {
      try {
        const res = await fetch(href);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const len = res.headers.get("content-length");
        if (len && Number(len) > MAX_PREVIEW_BYTES) {
          if (!cancelled) setState({ status: "toolarge" });
          return;
        }
        const text = await res.text();
        if (text.length > MAX_PREVIEW_BYTES) {
          if (!cancelled) setState({ status: "toolarge" });
          return;
        }
        if (!cancelled) setState({ status: "ready", text });
      } catch (e) {
        if (!cancelled) setState({ status: "error", message: e instanceof Error ? e.message : "load failed" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [href]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Window-like chrome: drag by the title bar, resize by the native bottom-right
  // handle (CSS resize). Size is a preference about YOUR screen, so it persists
  // globally (same rule as the chat panel height); position resets per open —
  // a remembered off-screen position would be a lost window.
  const panelRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const dragFrom = useRef<{ px: number; py: number; x: number; y: number } | null>(null);
  const onDragStart = useCallback((e: React.PointerEvent) => {
    const el = panelRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    dragFrom.current = { px: e.clientX, py: e.clientY, x: r.left, y: r.top };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  }, []);
  const onDragMove = useCallback((e: React.PointerEvent) => {
    const d = dragFrom.current;
    if (!d) return;
    setPos({
      x: Math.min(Math.max(d.x + e.clientX - d.px, 8 - (panelRef.current?.offsetWidth ?? 0) + 80), window.innerWidth - 80),
      y: Math.min(Math.max(d.y + e.clientY - d.py, 0), window.innerHeight - 40),
    });
  }, []);
  const onDragEnd = useCallback(() => {
    dragFrom.current = null;
    const el = panelRef.current;
    if (el) {
      try {
        localStorage.setItem("conductor.previewSize", JSON.stringify({ w: el.offsetWidth, h: el.offsetHeight }));
      } catch {
        /* preference only */
      }
    }
  }, []);
  const savedSize = (() => {
    try {
      const v = JSON.parse(localStorage.getItem("conductor.previewSize") || "null");
      if (v && typeof v.w === "number" && typeof v.h === "number") return v as { w: number; h: number };
    } catch {
      /* default */
    }
    return null;
  })();

  return (
    <div
      className={`fixed inset-0 z-[70] bg-black/50 ${pos ? "" : "flex items-center justify-center"} p-4`}
      onClick={onClose}
    >
      <div
        ref={panelRef}
        className="bg-zinc-900 border border-zinc-700 rounded-xl shadow-2xl flex flex-col overflow-hidden resize"
        style={{
          width: savedSize?.w ?? Math.min(window.innerWidth - 32, 768),
          height: savedSize?.h ?? Math.round(window.innerHeight * 0.85),
          minWidth: 320,
          minHeight: 240,
          maxWidth: "calc(100vw - 16px)",
          maxHeight: "calc(100vh - 16px)",
          ...(pos ? { position: "fixed" as const, left: pos.x, top: pos.y } : {}),
        }}
        onClick={(e) => e.stopPropagation()}
        onPointerUp={onDragEnd}
      >
        <div
          className="flex items-center gap-2 px-3 py-2 border-b border-zinc-800 shrink-0 cursor-move select-none touch-none"
          onPointerDown={onDragStart}
          onPointerMove={onDragMove}
          onPointerUp={onDragEnd}
        >
          <span className="text-sm font-medium text-zinc-100 truncate flex-1">{filename}</span>
          <a
            href={href}
            download={filename}
            className="text-xs text-sky-400 hover:text-sky-300 px-1.5 py-0.5 rounded"
            title="下載"
          >
            ⬇ 下載
          </a>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-100 px-1.5 py-0.5 rounded"
            title="關閉 (Esc)"
            aria-label="Close preview"
          >
            ✕
          </button>
        </div>
        <div className="flex-1 min-h-0 overflow-auto bg-zinc-950">
          {state.status === "loading" && (
            <div className="p-4 text-xs text-zinc-500">載入中…</div>
          )}
          {state.status === "toolarge" && (
            <div className="p-4 text-xs text-zinc-400 space-y-2">
              <div>檔案過大，無法預覽。</div>
              <a href={href} download={filename} className="text-sky-400 hover:text-sky-300">
                ⬇ 下載 {filename}
              </a>
            </div>
          )}
          {state.status === "error" && (
            <div className="p-4 text-xs text-zinc-400 space-y-2">
              <div>預覽失敗：{state.message}</div>
              <a href={href} download={filename} className="text-sky-400 hover:text-sky-300">
                ⬇ 下載 {filename}
              </a>
            </div>
          )}
          {state.status === "ready" && kind === "markdown" && (
            <div className="p-4 text-sm text-zinc-200">
              <Markdown text={state.text} />
            </div>
          )}
          {state.status === "ready" && kind === "text" && (
            <pre className="p-4 text-xs leading-relaxed text-zinc-200 whitespace-pre-wrap break-words font-mono">
              {prettyText(filename, state.text)}
            </pre>
          )}
          {state.status === "ready" && kind === "html" && (
            // No allow-same-origin: agent-authored reports may carry inline JS/charts —
            // scripts run, but the frame gets no cookies/origin to act on. White bg
            // default since these reports are usually light-styled documents.
            <iframe
              srcDoc={state.text}
              sandbox="allow-scripts"
              title={filename}
              className="w-full h-full bg-white border-0"
            />
          )}
        </div>
      </div>
    </div>
  );
}
