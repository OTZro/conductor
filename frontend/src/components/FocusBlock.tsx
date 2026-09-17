import type { ReactNode } from "react";

/**
 * Block-focus (⤢) wrapper for a card's content sections — the shared
 * affordance CardDetail's own sections AND PluginCardSlot's widget sections
 * use (there is no common section-header component in CardDetail, so the
 * pattern is: wrap the whole `<section>` and let the wrapper pin a
 * hover-revealed ⤢ at the block's top-right, level with the section header).
 *
 * Clicking ⤢ focuses the block: every OTHER wrapped block hides
 * (display:none — component state and live iframes survive) and this one
 * becomes an absolutely-positioned layer over the card's FULL height
 * (CardDetail's root is position:fixed and every intermediate wrapper is
 * static, so `inset-0` spans the whole card, sticky header included). A slim
 * breadcrumb row (⤡ 返回全部區塊 · title) is the way back.
 *
 * The wrapper div is ALWAYS the same element (className swap only), so
 * focusing never remounts children — a live terminal keeps its buffer.
 * `grow` swaps the focused layer from scrolling to a flex column, for blocks
 * (the terminal host) that stretch a child to the full height instead.
 *
 * Focus state is per-card-instance VIEW state: the host owns `focus`, resets
 * it on card switch, and nothing is persisted.
 *
 * SLIM-CHROME PATTERN: while focused, a block IS the whole card, so a
 * section's own single-accordion wrapper (the sessions picker, a plugin's
 * 「對話」collapse) is meaningless — hosts fold such chrome behind
 * `breadcrumbExtra` (one merged row, never stacked headers), and plugin
 * layouts receive `focused` via PluginCardProps to skip their internal
 * collapse header and flex-fill the height instead.
 */
export function FocusBlock({
  id,
  title,
  focus,
  setFocus,
  grow = false,
  className = "",
  breadcrumbExtra,
  children,
}: {
  id: string;
  title: string;
  focus: string | null;
  setFocus: (v: string | null) => void;
  grow?: boolean;
  className?: string;
  /** right-aligned content on the SAME breadcrumb row (e.g. the sessions
   * accordion toggle) — one slim row, never two stacked ones. */
  breadcrumbExtra?: ReactNode;
  children: ReactNode;
}) {
  const focused = focus === id;
  const hidden = focus !== null && !focused;
  return (
    <div
      // focused: `!m-0` matters — the parent's space-y-* would otherwise
      // apply its margin to this absolutely-positioned box and shove the
      // whole overlay down (the "dead band" above the breadcrumb); padding
      // is kept slim for the same reason.
      className={
        focused
          ? `absolute inset-0 z-30 !m-0 bg-surface-raised px-3 pt-1.5 pb-3 flex flex-col min-h-0 ${
              grow ? "overflow-hidden" : "overflow-y-auto"
            }`
          : `relative group/fb ${hidden ? "hidden" : ""} ${className}`
      }
    >
      {focused ? (
        <div className="shrink-0 mb-2 flex items-center gap-2 border-b border-zinc-800 pb-1.5">
          <button
            onClick={() => setFocus(null)}
            className="px-1.5 py-0.5 rounded text-xs bg-zinc-800 border border-zinc-700 text-zinc-300 hover:text-zinc-100 hover:bg-zinc-700"
            title="還原所有區塊"
          >
            ⤡ 返回全部區塊
          </button>
          <span className="min-w-0 truncate text-[11px] uppercase tracking-wide text-zinc-500">
            {title}
          </span>
          {breadcrumbExtra && <div className="ml-auto shrink-0">{breadcrumbExtra}</div>}
        </div>
      ) : (
        <button
          onClick={() => setFocus(id)}
          className="absolute right-0 top-0 z-10 px-1 py-0.5 rounded text-[11px] text-zinc-500 opacity-0 group-hover/fb:opacity-100 focus-visible:opacity-100 hover:text-zinc-200 hover:bg-zinc-700/60"
          title="只看這個區塊(佔滿整張卡)"
          aria-label={`Focus ${title}`}
        >
          ⤢
        </button>
      )}
      {children}
    </div>
  );
}
