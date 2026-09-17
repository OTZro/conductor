import type { ButtonHTMLAttributes, ReactNode } from "react";

// The component library (see docs/IMPROVEMENT_PLAN.md §3A.4). Rules:
// - semantic tokens only (src-*/state-*/sev-*/host-*, surface-*), never raw palette
// - no font below text-caption (11px)
// - every interactive element ≥44px touch target on mobile (p-* or ::after pad)

/* ── Chip ──────────────────────────────────────────────────────────────────── */

const CHIP_TONES = {
  jira: "bg-src-jira/20 text-src-jira",
  pr: "bg-src-pr/20 text-src-pr",
  slack: "bg-src-slack/20 text-src-slack",
  manual: "bg-src-manual/20 text-src-manual",
  human: "bg-state-human/20 text-state-human",
  ai: "bg-state-ai/20 text-state-ai",
  done: "bg-state-done/30 text-zinc-400",
  urgent: "bg-sev-urgent/20 text-sev-urgent",
  warn: "bg-sev-warn/20 text-sev-warn",
  ok: "bg-sev-ok/20 text-sev-ok",
  local: "bg-host-local/20 text-host-local",
  remote: "bg-host-remote/25 text-host-remote",
  neutral: "bg-surface-hover/60 text-zinc-300",
} as const;

export type ChipTone = keyof typeof CHIP_TONES;

export function Chip({
  tone = "neutral",
  title,
  className = "",
  children,
}: {
  tone?: ChipTone;
  title?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 text-caption font-semibold px-1.5 py-0.5 rounded-chip ${CHIP_TONES[tone]} ${className}`}
    >
      {children}
    </span>
  );
}

/* ── StatusDot ─────────────────────────────────────────────────────────────── */

const DOT_TONES = {
  human: "bg-state-human",
  ai: "bg-state-ai",
  done: "bg-state-done",
  ok: "bg-sev-ok",
  warn: "bg-sev-warn",
  urgent: "bg-sev-urgent",
  off: "bg-zinc-600",
} as const;

export function StatusDot({
  tone,
  title,
  className = "",
}: {
  tone: keyof typeof DOT_TONES;
  title?: string;
  className?: string;
}) {
  return <span title={title} className={`inline-block w-2 h-2 rounded-full ${DOT_TONES[tone]} ${className}`} />;
}

/* ── Btn ───────────────────────────────────────────────────────────────────── */

const BTN_VARIANTS = {
  primary: "bg-sky-600 hover:bg-sky-500 text-white",
  accent: "bg-state-ai/15 hover:bg-state-ai/25 border border-state-ai/30 text-sky-200",
  ok: "bg-sev-ok/15 hover:bg-sev-ok/25 border border-sev-ok/30 text-emerald-200",
  ghost: "bg-surface-hover hover:bg-zinc-700 border border-zinc-700 text-zinc-300",
  danger: "bg-red-600/80 hover:bg-red-600 text-white border border-red-500",
} as const;

export function Btn({
  variant = "ghost",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof BTN_VARIANTS }) {
  return (
    <button
      {...props}
      className={`text-body-s px-2.5 py-1.5 rounded-chip transition-colors duration-fast disabled:opacity-50 ${BTN_VARIANTS[variant]} ${className}`}
    />
  );
}

/* ── SectionHeader ─────────────────────────────────────────────────────────── */

export function SectionHeader({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <h3 className="text-caption font-semibold text-zinc-400 uppercase tracking-wide">{children}</h3>
      {right && <div className="ml-auto flex items-center gap-1.5">{right}</div>}
    </div>
  );
}

/* ── EmptyState ────────────────────────────────────────────────────────────── */

export function EmptyState({ icon = "·", children }: { icon?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 py-6 text-zinc-600 select-none">
      <span className="text-title">{icon}</span>
      <span className="text-body-s">{children}</span>
    </div>
  );
}

/* ── Skeleton ──────────────────────────────────────────────────────────────── */

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-card bg-surface-hover/50 ${className}`} />;
}

export function CardSkeleton() {
  return (
    <div className="rounded-card border border-zinc-800 bg-surface-raised/70 p-3 space-y-2">
      <Skeleton className="h-3 w-24" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-3 w-2/3" />
    </div>
  );
}

/* ── KeyHint ───────────────────────────────────────────────────────────────── */

export function KeyHint({ k }: { k: string }) {
  return (
    <kbd className="text-caption font-mono px-1 py-px rounded bg-surface-hover border border-zinc-700 text-zinc-400">
      {k}
    </kbd>
  );
}

/* ── OFFSCREEN_SKIP ────────────────────────────────────────────────────────── */

/** Let the browser skip layout+paint for a list item scrolled out of view.
 *
 *  THE INVARIANT, because it is not local to the element that carries this:
 *  `content-visibility: auto` implies `contain: layout style paint`, so anything an
 *  item renders that visually escapes its own box — a popover, tooltip, dropdown —
 *  MUST render through a portal or it is clipped. Not merely clipped, either:
 *  measured, an overflowing child returns `null` from `elementFromPoint`, so it is
 *  untargetable too. `ui/HoverCard.tsx` satisfies this via `createPortal` + `fixed`.
 *  It lives here, in `ui/`, so the person ADDING an overlay to a card face meets it —
 *  a comment on the consumer is only read by someone who already satisfies it.
 *
 *  The size is the *content* box (padding and border sit outside it), and it is a
 *  single length so it applies to both axes — fine only for a full-width item.
 *  8rem = 128px content ≈ 154px border-box against measured cards of 116/136/152/200px;
 *  `auto` replaces the estimate with the real height once an item has been seen, so a
 *  wrong guess costs scrollbar drift on first pass, not permanently. */
export const OFFSCREEN_SKIP = "[content-visibility:auto] [contain-intrinsic-size:auto_8rem]";
