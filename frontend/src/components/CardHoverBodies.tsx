import { useState } from "react";
import { getJiraStatuses, transitionJira } from "../api";
import { splitReviewers } from "../cardMeta";
import type { Card } from "../types";

// Shared hover-card bodies for PR / Slack / Jira / generic link meta — used by the
// detail's LinksPanel AND the board card's small tags, so hovering either surface
// shows the same rich info (per-surface drift is exactly the bug class to avoid).

// PR status shape stored in cached.prs (jira card's own PRs) and mirrored into a
// linked ticket's cached.jiras[ref].prs — one shared type so both surfaces read it
// the same way.
export type PrMeta = {
  state?: string;
  ci?: string;
  review?: string;
  checks?: { name: string; state: string }[];
  reviews?: { user: string; state: string }[];
  review_requests?: string[];
};

export type JiraMeta = {
  title?: string | null;
  status?: string | null;
  assignee?: string | null;
  // the linked ticket's PRs (mirrored from its jira-origin card's cached.prs) — absent
  // when that card doesn't exist yet or has none.
  prs?: Record<string, PrMeta>;
};

// One resolver for a jira chip's hover data, shared by every surface: the card's OWN
// ticket reads the card itself (zero fetch); linked tickets read cached.jiras
// (enriched however the link got attached). undefined → render a plain chip.
export function jiraHoverMeta(card: Card, ref: string): JiraMeta | undefined {
  if (card.origin === "jira" && ref === card.external_id) {
    return { title: card.title, status: (card.cached?.jira || {}).status as string | undefined };
  }
  const jiras = (card.cached?.jiras || {}) as Record<string, JiraMeta>;
  return jiras[ref];
}

export const REVIEW_ICON: Record<string, string> = {
  approved: "✓",
  changes: "✗",
  commented: "💬",
  dismissed: "⊘",
  pending: "⏳",
};
export const REVIEW_CLS: Record<string, string> = {
  approved: "text-emerald-300",
  changes: "text-red-300",
  commented: "text-amber-200",
  dismissed: "text-zinc-400",
  pending: "text-zinc-400",
};
export const CI_CLS: Record<string, string> = {
  passing: "text-emerald-300",
  failing: "text-red-300",
  pending: "text-amber-200",
};

// A linked jira ticket's preview (title · status · assignee) — data from cached.jiras
// (linked tickets, enriched) or, for a jira card's own chip, from the card itself.
export function JiraTooltipBody({
  ticket,
  title,
  status,
  assignee,
}: {
  ticket: string;
  title?: string | null;
  status?: string | null;
  assignee?: string | null;
}) {
  // status transition straight from the hover (Building → Hardening …). The picker
  // offers the board's real status vocabulary; jira validates the move server-side
  // and its refusal (if any) shows verbatim. Board/hover data refresh over the WS.
  const [open, setOpen] = useState(false);
  const [choices, setChoices] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const openPicker = () => {
    setOpen((v) => !v);
    if (!choices)
      getJiraStatuses()
        .then(setChoices)
        .catch(() => setChoices([]));
  };
  const go = async (to: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await transitionJira(ticket, to);
      setMsg(`✓ → ${to}`);
      setOpen(false);
    } catch (e: any) {
      setMsg(`⚠ ${e?.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-1.5">
      <div className="text-[10px] uppercase tracking-wide text-blue-300">{ticket}</div>
      {title && <div className="text-zinc-100">{title}</div>}
      <div className="flex items-center gap-2 text-zinc-300">
        {status && <span>◔ {status}</span>}
        {assignee && <span className="text-zinc-400">👤 {assignee}</span>}
        <button
          onClick={openPicker}
          disabled={busy}
          className="ml-auto px-1.5 py-0.5 rounded border border-zinc-700 text-[10px] text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
          title="move this ticket to another status"
        >
          ⇄ status
        </button>
      </div>
      {open && (
        <div className="flex flex-wrap gap-1 pt-0.5">
          {choices === null ? (
            <span className="text-zinc-500 text-[10px]">loading…</span>
          ) : choices.length === 0 ? (
            <span className="text-zinc-500 text-[10px]">no statuses known yet</span>
          ) : (
            choices
              .filter((s) => s !== status)
              .map((s) => (
                <button
                  key={s}
                  disabled={busy}
                  onClick={() => go(s)}
                  className="px-1.5 py-0.5 rounded border border-blue-500/30 bg-blue-500/10 text-[10px] text-blue-200 hover:bg-blue-500/20 disabled:opacity-50"
                >
                  {s}
                </button>
              ))
          )}
        </div>
      )}
      {msg && <div className={msg.startsWith("✓") ? "text-emerald-300" : "text-red-300"}>{msg}</div>}
      {!title && !status && <div className="text-zinc-400">loading ticket info…</div>}
    </div>
  );
}


// Generic hover for a PLUGIN-enriched link kind (cached.linkmeta[kind][ref] — see
// LinkEnricherSpec): title / status / free-form lines. Bespoke kinds (pr/slack/jira)
// keep their richer bodies above.
export function GenericLinkTooltipBody({
  refName,
  meta,
}: {
  refName: string;
  meta: { title?: string; status?: string; lines?: string[] };
}) {
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wide text-zinc-400">{refName}</div>
      {meta.title && <div className="text-zinc-100">{meta.title}</div>}
      {meta.status && <div className="text-zinc-300">◔ {meta.status}</div>}
      {(meta.lines || []).map((ln, i) => (
        <div key={i} className="text-zinc-400">{ln}</div>
      ))}
    </div>
  );
}


export function PrTooltipBody({
  ci,
  review,
  checks,
  reviews,
  reqs,
}: {
  ci: string;
  review?: string;
  checks: { name: string; state: string }[];
  reviews: { user: string; state: string }[];
  reqs: string[];
}) {
  const { humans, ai } = splitReviewers(reviews);
  const row = (r: { user: string; state: string }) => (
    <div key={r.user} className={REVIEW_CLS[r.state] || "text-zinc-300"}>
      {REVIEW_ICON[r.state] || "·"} {r.user}
      <span className="text-zinc-300"> · {r.state}</span>
    </div>
  );
  return (
    <div className="space-y-2">
      <div>
        <div className={`text-[10px] uppercase tracking-wide mb-0.5 ${CI_CLS[ci] || "text-zinc-300"}`}>
          CI · {ci}
        </div>
        {checks.length === 0 ? (
          <div className={ci === "passing" ? "text-emerald-300/90" : "text-zinc-300"}>
            no failing / pending checks
          </div>
        ) : (
          <div className="space-y-0.5">
            {checks.map((c, i) => (
              <div key={i} className={c.state === "failing" ? "text-red-300" : "text-amber-200"}>
                {c.state === "failing" ? "✗" : "⏳"} {c.name}
              </div>
            ))}
          </div>
        )}
      </div>
      {(humans.length > 0 || reqs.length > 0) && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-zinc-300 mb-0.5">
            👤 Reviewers{review ? ` · ${review.replace(/_/g, " ")}` : ""}
          </div>
          <div className="space-y-0.5">
            {humans.map(row)}
            {reqs.map((u) => (
              <div key={u} className="text-zinc-400">
                ⏳ {u} · requested
              </div>
            ))}
          </div>
        </div>
      )}
      {ai.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-zinc-300 mb-0.5">🤖 AI agents</div>
          <div className="space-y-0.5">{ai.map(row)}</div>
        </div>
      )}
    </div>
  );
}

export function SlackTooltipBody({ from, text }: { from?: string; text?: string }) {
  return (
    <div className="space-y-1">
      {from && <div className="font-semibold text-zinc-100">{from}</div>}
      {text && (
        <div className="whitespace-pre-wrap leading-relaxed text-zinc-300">
          {String(text).slice(0, 800)}
          {String(text).length > 800 ? "…" : ""}
        </div>
      )}
    </div>
  );
}
