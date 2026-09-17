import { type ReactNode, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// A component tooltip (hover card) — richer than the native `title`: renders arbitrary
// JSX in a floating, styled panel PORTALED to <body> (never clipped, valid inside a
// <button>). Caps its height to the space available and scrolls the overflow.
//
// Desktop (hover-capable): opens on hover, and a click on the wrapped link navigates
// as usual — a 300ms hide delay lets you move onto the panel to read/scroll it.
//
// Touch (coarse pointer / no hover): tapping the trigger PEEKS (opens the panel)
// instead of following the link — intercepted in the capture phase so it beats both
// the inner <a> and the surrounding card button. Navigation is then an explicit
// "open ↗" button in the panel (shown when `href` is set); tap-outside or a second
// tap on the chip dismisses. This gives "peek first, leave the app deliberately".
const IS_TOUCH =
  typeof window !== "undefined" && !!window.matchMedia?.("(hover: none)").matches;

export function HoverCard({
  children,
  content,
  href,
  openLabel = "open ↗",
  width = 320,
  className = "inline-flex",
  hoverOnly = false,
}: {
  children: ReactNode; // the trigger
  content: ReactNode; // the rich tooltip body
  href?: string; // when set, the panel shows an explicit open-link button (mobile's nav)
  openLabel?: string;
  width?: number;
  className?: string; // trigger wrapper class (e.g. "block w-full" to wrap a full-width row)
  hoverOnly?: boolean; // desktop-hover only — don't intercept touch taps (let them through)
}) {
  const [box, setBox] = useState<{ left: number; top: number; flip: boolean; maxH: number } | null>(
    null,
  );
  const ref = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const showT = useRef<ReturnType<typeof setTimeout>>();
  const hideT = useRef<ReturnType<typeof setTimeout>>();

  const place = () => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
    const spaceBelow = window.innerHeight - r.bottom - 12;
    const spaceAbove = r.top - 12;
    // open downward by default; flip up only when below is cramped and above is roomier
    const flip = spaceBelow < 220 && spaceAbove > spaceBelow;
    const maxH = Math.max(140, flip ? spaceAbove : spaceBelow);
    setBox({ left, top: flip ? r.top - 6 : r.bottom + 6, flip, maxH });
  };
  const show = () => {
    clearTimeout(hideT.current);
    showT.current = setTimeout(place, 100);
  };
  const hide = () => {
    clearTimeout(showT.current);
    hideT.current = setTimeout(() => setBox(null), 300); // grace period to move onto the panel
  };
  const close = () => {
    clearTimeout(showT.current);
    clearTimeout(hideT.current);
    setBox(null);
  };

  // touch: toggle the peek panel in the CAPTURE phase — before the inner <a> navigates
  // or the card button opens the drawer. Navigation happens only via the "open ↗" button.
  const onClickCapture = (e: React.MouseEvent) => {
    if (!IS_TOUCH || hoverOnly) return; // desktop / hoverOnly: let the click flow through
    // the portaled panel bubbles its clicks HERE via the React tree (not the DOM
    // tree) — don't intercept those, or the "open ↗" button's navigation is killed.
    if (panelRef.current?.contains(e.target as Node)) return;
    e.preventDefault();
    e.stopPropagation();
    if (box) close();
    else place();
  };
  useEffect(() => {
    if (!box || !IS_TOUCH) return;
    const onDown = (e: Event) => {
      const t = e.target as Node;
      if (ref.current?.contains(t) || panelRef.current?.contains(t)) return;
      close();
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [box]);

  return (
    <span
      ref={ref}
      onClickCapture={onClickCapture}
      onMouseEnter={IS_TOUCH ? undefined : show}
      onMouseLeave={IS_TOUCH ? undefined : hide}
      className={className}
    >
      {children}
      {/* The portal is load-bearing, not just convenient: CardItem sets
          `content-visibility: auto`, whose implied paint containment would clip an
          in-tree popover to the card's own box. Rendering to document.body keeps the
          panel outside that containment. Keep it portalled + `fixed`. */}
      {box &&
        createPortal(
          <div
            ref={panelRef}
            onMouseEnter={IS_TOUCH ? undefined : () => clearTimeout(hideT.current)}
            onMouseLeave={IS_TOUCH ? undefined : hide}
            className="fixed z-[80] overflow-y-auto rounded-lg border border-zinc-700 bg-zinc-900/98 p-2.5 text-[11px] text-zinc-200 shadow-2xl backdrop-blur"
            style={{
              left: box.left,
              top: box.top,
              width,
              maxHeight: box.maxH,
              transform: box.flip ? "translateY(-100%)" : undefined,
            }}
          >
            {content}
            {href && (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => {
                  e.stopPropagation();
                  close();
                }}
                className="mt-2 flex items-center justify-center text-[11px] px-2 py-1.5 rounded bg-sky-600/80 hover:bg-sky-500 text-white font-medium"
              >
                {openLabel}
              </a>
            )}
          </div>,
          document.body,
        )}
    </span>
  );
}
