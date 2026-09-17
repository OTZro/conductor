import type { Card } from "../types";
import { HoverCard } from "../ui/HoverCard";
import { GenericLinkTooltipBody, JiraTooltipBody, PrTooltipBody, SlackTooltipBody, jiraHoverMeta } from "./CardHoverBodies";

const KIND_STYLE: Record<string, string> = {
  jira: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  pr: "bg-purple-500/15 text-purple-300 border-purple-500/30",
  slack: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  url: "bg-zinc-500/15 text-zinc-300 border-zinc-500/30",
};
// review decision → a compact badge on the PR chip (the same review info the job detail
// shows) so you read review state without hovering.
const REVIEW_DECISION: Record<string, { icon: string; cls: string; label: string }> = {
  approved: { icon: "✓", cls: "text-emerald-300", label: "approved" },
  changes_requested: { icon: "✗", cls: "text-red-300", label: "changes requested" },
  review_required: { icon: "◔", cls: "text-amber-300", label: "review required" },
  commented: { icon: "💬", cls: "text-amber-200", label: "commented" },
};

type PrMeta = {
  state?: string;
  ci?: string;
  review?: string;
  checks?: { name: string; state: string }[];
  reviews?: { user: string; state: string }[];
  review_requests?: string[];
};

type ChipItem = { kind: string; ref: string; url: string; title?: string | null; meta?: PrMeta };

// Board card's link tags — a compact LinksPanel. PR/Slack chips get the same rich
// HoverCard (CI checks + reviewers / sender + message) so you read them without opening
// the card. PR data comes from cached.prs (a jira card's linked PRs) — or, for a github
// (PR job) card, from cached.github for its OWN PR (whose self-link is filtered off
// card.links) so a PR job reads like a jira card's PR link.
export function LinkChips({ card }: { card: Card }) {
  const prMeta = (card.cached?.prs || {}) as Record<string, PrMeta>;
  const gh = (card.cached?.github || {}) as PrMeta & { number?: number; repo?: string; url?: string };
  const sl = card.cached?.slack || {};
  // plugin-enriched link meta for kinds without a bespoke body (LinkEnricherSpec)
  const linkmeta = (card.cached?.linkmeta || {}) as Record<
    string,
    Record<string, { title?: string; status?: string; lines?: string[] }>
  >;

  const selfPr: ChipItem[] =
    card.origin === "github"
      ? [{
          kind: "pr",
          ref: card.external_id,
          url:
            card.url ||
            gh.url ||
            (gh.repo && gh.number ? `https://github.com/${gh.repo}/pull/${gh.number}` : ""),
          title: card.external_id,
          meta: gh,
        }]
      : [];
  const rest: ChipItem[] = card.links.map((l) => ({
    ...l,
    meta: l.kind === "pr" ? prMeta[l.ref] : undefined,
  }));
  // the board lists only OPEN PRs — hide merged + closed (state unknown = keep, may be open)
  const items = [...selfPr, ...rest].filter(
    (l) => !(l.kind === "pr" && (l.meta?.state === "merged" || l.meta?.state === "closed")),
  );
  if (!items.length) return null;

  return (
    <div className="flex flex-wrap gap-1">
      {items.map((l) => {
        const key = l.kind + l.ref;
        const cls = `text-[10px] px-1.5 py-0.5 rounded border ${KIND_STYLE[l.kind] || KIND_STYLE.url}`;
        const m = l.kind === "pr" ? l.meta : undefined;
        const rev = m?.review ? REVIEW_DECISION[m.review] : undefined;
        // label = "PR repo#id" + a review badge (they're all open → no state on the label)
        const anchor = (title?: string) => (
          <a href={l.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} title={title} className={cls}>
            {l.kind === "pr" ? "PR " : ""}
            {l.title || l.ref}
            {rev && (
              <span className={`ml-1 ${rev.cls}`} title={rev.label}>
                {rev.icon}
              </span>
            )}
          </a>
        );
        const prDetail =
          m && ((m.checks?.length ?? 0) > 0 || (m.reviews?.length ?? 0) > 0 || (m.review_requests?.length ?? 0) > 0);
        if (prDetail) {
          return (
            <HoverCard
              key={key}
              href={l.url}
              openLabel="open PR ↗"
              content={
                <PrTooltipBody
                  ci={m!.ci || "none"}
                  review={m!.review}
                  checks={m!.checks || []}
                  reviews={m!.reviews || []}
                  reqs={m!.review_requests || []}
                />
              }
            >
              {anchor()}
            </HoverCard>
          );
        }
        if (l.kind === "slack" && (sl.from || sl.text)) {
          return (
            <HoverCard
              key={key}
              width={360}
              href={l.url}
              openLabel="open in Slack ↗"
              content={<SlackTooltipBody from={sl.from} text={sl.text} />}
            >
              {anchor()}
            </HoverCard>
          );
        }
        if (l.kind === "jira") {
          // every jira chip gets a hover — the card's own ticket reads the card,
          // linked tickets read cached.jiras (enriched however the link got here)
          const jm = jiraHoverMeta(card, l.ref);
          if (jm) {
            return (
              <HoverCard
                key={key}
                href={l.url}
                openLabel="open in Jira ↗"
                content={
                  <JiraTooltipBody
                    ticket={l.ref}
                    title={jm.title}
                    status={jm.status}
                    assignee={(jm as { assignee?: string }).assignee}
                  />
                }
              >
                {anchor()}
              </HoverCard>
            );
          }
        }
        {
          // plugin-enriched kinds (zendesk / sentry / …) get the generic hover
          const gm = linkmeta[l.kind]?.[l.ref];
          if (gm) {
            return (
              <HoverCard key={key} href={l.url} openLabel="open ↗"
                content={<GenericLinkTooltipBody refName={l.ref} meta={gm} />}>
                {anchor()}
              </HoverCard>
            );
          }
        }
        return <span key={key}>{anchor(l.url)}</span>;
      })}
    </div>
  );
}
