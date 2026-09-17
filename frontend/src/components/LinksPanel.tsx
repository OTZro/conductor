import { Fragment, type ReactNode } from "react";
import type { TSlackMsg } from "../api";
import { splitReviewers } from "../cardMeta";
import type { Card, CardLink } from "../types";
import { HoverCard } from "../ui/HoverCard";
import {
  GenericLinkTooltipBody,
  JiraTooltipBody,
  type PrMeta,
  PrTooltipBody,
  SlackTooltipBody,
  jiraHoverMeta,
} from "./CardHoverBodies";

// a slack permalink ends in /p<epoch><6-frac> — recover the message ts so each link
// maps to ITS message in the thread (two links on one card are different messages).
function permalinkTs(url?: string | null): string | null {
  const d = url?.match(/\/p(\d+)/)?.[1];
  return d && d.length > 6 ? `${d.slice(0, -6)}.${d.slice(-6)}` : null;
}

const STATE_BADGE: Record<string, string> = {
  open: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  merged: "bg-purple-500/15 text-purple-300 border-purple-500/30",
  closed: "bg-zinc-600/30 text-zinc-400 border-zinc-600/40",
  declined: "bg-red-500/15 text-red-300 border-red-500/30",
};
const CI_DOT: Record<string, string> = {
  passing: "bg-emerald-400",
  failing: "bg-red-400",
  pending: "bg-amber-400",
  none: "bg-zinc-600",
};
// a linked ticket's PR ref is "owner/repo#123" (no url stored — build the same GitHub
// URL shape a jira card's own auto-discovered PR links use).
function prRefUrl(ref: string): string {
  const [repo, num] = ref.split("#");
  return repo && num ? `https://github.com/${repo}/pull/${num}` : "";
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wide text-zinc-500">{title}</div>
      <div className="flex flex-wrap gap-1.5">{children}</div>
    </div>
  );
}


function PrChip({ link, meta }: { link: CardLink; meta?: PrMeta }) {
  const state = meta?.state || "";
  const ci = meta?.ci || "none";
  const checks = meta?.checks || [];
  const reviews = meta?.reviews || [];
  const reqs = meta?.review_requests || [];
  const { humans, ai } = splitReviewers(reviews);
  const nFail = checks.filter((c) => c.state === "failing").length;
  const nPend = checks.filter((c) => c.state === "pending").length;
  // verdict counts come from HUMAN reviewers — AI (Copilot/CodeRabbit/…) is advisory
  const nApprove = humans.filter((r) => r.state === "approved").length;
  const nChanges = humans.filter((r) => r.state === "changes").length;
  const hasDetail = checks.length > 0 || reviews.length > 0 || reqs.length > 0;

  const chip = (
    <a
      href={link.url}
      target="_blank"
      rel="noreferrer"
      onClick={(e) => e.stopPropagation()}
      className="flex items-center gap-1.5 text-[11px] px-2 py-1 rounded border bg-zinc-800/60 border-zinc-700 hover:bg-zinc-800"
    >
      <span className="font-mono text-zinc-200">{link.ref}</span>
      {state && (
        <span className={`text-[9px] px-1 rounded border ${STATE_BADGE[state] || STATE_BADGE.closed}`}>
          {state}
        </span>
      )}
      {/* CI: colored dot + failing/pending count */}
      <span className="flex items-center gap-0.5">
        <span className={`w-1.5 h-1.5 rounded-full ${CI_DOT[ci] || CI_DOT.none}`} />
        {nFail > 0 && <span className="text-red-300">✗{nFail}</span>}
        {nFail === 0 && nPend > 0 && <span className="text-amber-300">⏳{nPend}</span>}
      </span>
      {/* human review verdict: changes wins, else approvals, else pending reviewer count */}
      {nChanges > 0 ? (
        <span className="text-red-300">✗ changes</span>
      ) : nApprove > 0 ? (
        <span className="text-emerald-300">✓{nApprove}</span>
      ) : humans.length + reqs.length > 0 ? (
        <span className="text-zinc-400">👀{humans.length + reqs.length}</span>
      ) : null}
      {ai.length > 0 && <span className="text-zinc-500">🤖{ai.length}</span>}
    </a>
  );

  if (!hasDetail) return chip;
  return (
    <HoverCard
      href={link.url}
      openLabel="open PR ↗"
      content={
        <PrTooltipBody ci={ci} review={meta?.review} checks={checks} reviews={reviews} reqs={reqs} />
      }
    >
      {chip}
    </HoverCard>
  );
}

export function LinksPanel({ card, slackThread }: { card: Card; slackThread?: TSlackMsg[] }) {
  const groups: Record<string, CardLink[]> = { jira: [], pr: [], slack: [], url: [] };
  card.links.forEach((l) => (groups[l.kind] || groups.url).push(l));
  const prMeta = (card.cached?.prs || {}) as Record<string, PrMeta>;
  const jiraStatus = card.cached?.jira?.status as string | undefined;

  if (!card.links.length) return null;

  return (
    <div className="space-y-2.5">
      {groups.pr.length > 0 && (
        <Section title="Pull Requests">
          {groups.pr.map((l) => (
            <PrChip key={l.ref} link={l} meta={prMeta[l.ref]} />
          ))}
        </Section>
      )}
      {groups.jira.length > 0 && (
        <Section title="Jira">
          {groups.jira.map((l) => {
            // same hover the board chip gets (shared resolver + body — one surface
            // must never drift from the other), incl. the ⇄ status transition
            const jm = jiraHoverMeta(card, l.ref);
            // a card that only LINKS to this ticket has no PR links of its own — mirror
            // the ticket's PRs (cached.jiras[ref].prs, copied from its jira-origin card's
            // cached.prs) using the SAME PrChip the "Pull Requests" section above renders.
            const linkedPrs = Object.entries(jm?.prs || {}) as [string, PrMeta][];
            const anchor = (
              <a
                href={l.url}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="flex items-center gap-1.5 text-[11px] px-2 py-1 rounded border bg-blue-500/10 border-blue-500/30 text-blue-200"
              >
                <span className="font-mono">{l.ref}</span>
                {(jm?.status || (l.ref === card.external_id && jiraStatus)) && (
                  <span className="text-[9px] px-1 rounded bg-blue-500/20">
                    {jm?.status || jiraStatus}
                  </span>
                )}
              </a>
            );
            const jiraNode = !jm ? (
              <span key={l.ref}>{anchor}</span>
            ) : (
              <HoverCard
                key={l.ref}
                href={l.url}
                openLabel="open in Jira ↗"
                content={
                  <JiraTooltipBody
                    ticket={l.ref}
                    title={jm.title}
                    status={jm.status}
                    assignee={jm.assignee}
                  />
                }
              >
                {anchor}
              </HoverCard>
            );
            if (!linkedPrs.length) return jiraNode;
            return (
              <Fragment key={l.ref}>
                {jiraNode}
                {linkedPrs.map(([ref, meta]) => (
                  <PrChip
                    key={ref}
                    link={{ kind: "pr", ref, url: prRefUrl(ref), auto: true }}
                    meta={meta}
                  />
                ))}
              </Fragment>
            );
          })}
        </Section>
      )}
      {groups.slack.length > 0 && (
        <Section title="Slack">
          {groups.slack.map((l) => {
            // match THIS link to its own message in the thread; fall back to the card's
            // cached message when the thread isn't loaded or the ts isn't found.
            const ts = permalinkTs(l.url);
            const msg = ts ? slackThread?.find((m) => m.ts === ts) : undefined;
            const sl = card.cached?.slack || {};
            const from = msg?.user || sl.from;
            const text = msg?.text || sl.text;
            const anchor = (
              <a
                href={l.url}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="text-[11px] px-2 py-1 rounded border bg-emerald-500/10 border-emerald-500/30 text-emerald-200"
              >
                💬 {from || l.title || "slack"}
              </a>
            );
            if (!from && !text) return <span key={l.ref}>{anchor}</span>;
            return (
              <HoverCard
                key={l.ref}
                width={360}
                href={l.url}
                openLabel="open in Slack ↗"
                content={<SlackTooltipBody from={from} text={text} />}
              >
                {anchor}
              </HoverCard>
            );
          })}
        </Section>
      )}
      {groups.url.length > 0 && (
        <Section title="Links">
          {groups.url.map((l) => {
            const anchor = (
              <a
                href={l.url}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="text-[11px] px-2 py-1 rounded border bg-zinc-700/40 border-zinc-700 text-zinc-300"
              >
                {l.title || l.ref}
              </a>
            );
            // plugin-enriched kinds (LinkEnricherSpec) get the generic hover here too
            const gm = (card.cached?.linkmeta || {})[l.kind]?.[l.ref] as
              | { title?: string; status?: string; lines?: string[] }
              | undefined;
            if (!gm) return <span key={l.ref}>{anchor}</span>;
            return (
              <HoverCard key={l.ref} href={l.url} openLabel="open ↗"
                content={<GenericLinkTooltipBody refName={l.ref} meta={gm} />}>
                {anchor}
              </HoverCard>
            );
          })}
        </Section>
      )}
    </div>
  );
}
