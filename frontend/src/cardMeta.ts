import type { Card } from "./types";

// Every helper below that depends on the wall clock takes `now` as a REQUIRED argument
// rather than reading Date.now() itself. Two reasons, both learned the hard way:
//
//   1. CardItem is memo'd, so it re-renders only when its card changes. A helper that
//      read the clock internally would silently freeze — "now" stuck at "now" an hour
//      later, an overdue card never turning red. Making `now` an argument means the
//      compiler asks every caller where its clock comes from, instead of the coupling
//      living in a comment. See clock.tsx.
//   2. Purity. Board's sort derives these keys once per card; a comparator that read
//      Date.now() per comparison could disagree with itself mid-sort, which is a
//      violated sort contract, not just wasted work.

// AI/bot code reviewers (Copilot, CodeRabbit, Sonar, …) vs humans — GitHub app
// reviewers usually carry a [bot] login suffix; the named ones don't, so match both.
const AI_REVIEWER_RE =
  /\[bot\]$|copilot|coderabbit|sonar|dependabot|renovate|codecov|gemini|greptile|bugbot|graphite|chatgpt|codex|devin|cursor|ellipsis|qodo|sweep|codeium/i;

export function isAiReviewer(login: string): boolean {
  return AI_REVIEWER_RE.test(login);
}

export function splitReviewers<T extends { user: string }>(reviews: T[]): { humans: T[]; ai: T[] } {
  const humans: T[] = [];
  const ai: T[] = [];
  for (const r of reviews) (isAiReviewer(r.user) ? ai : humans).push(r);
  return { humans, ai };
}

// The one-line "why is this card in this lane" (§3B.3). Scanning the board you
// read the title + this sentence; you shouldn't have to decode a row of badges.
// Precedence mirrors lanes.recompute_ball so the sentence matches the lane.

export function statusSentence(card: Card, now: number): string {
  const c = card.cached || {};
  const agent = c.agent || {};
  const gh = c.github || {};
  const jira = c.jira || {};
  const slack = c.slack || {};

  if (agent.bg) return `⏵ background: ${agent.bg}`;
  if (agent.running) return "claude running";
  if (agent.waiting) return agent.choice ? "claude waiting for your choice" : "claude waiting for your input";

  if (card.origin === "github") {
    const mine = gh.my_review as string | undefined;
    if (gh.state === "merged") return "merged";
    if (gh.state === "closed") return "closed";
    if (mine === "APPROVED") return gh.ci === "failing" ? "you approved, but CI is failing" : "you approved — awaiting merge";
    if (mine === "CHANGES_REQUESTED") return "you requested changes — waiting on the author";
    if (mine === "SUGGEST_APPROVE") return "suggests approve (you haven't formally approved)";
    if (gh.ci === "failing") return "CI failing — needs attention";
    if (gh.review_requested_me) return "awaiting your review";
    return "PR in progress";
  }

  if ((jira.labels || []).includes("conductor-hold")) return "waiting on you (hold)";

  if (card.origin === "slack") {
    const isDm = slack.kind === "dm";
    const kind = slack.kind && slack.kind !== "mention" ? slack.kind : "";
    if (slack.brief) return `Slack${kind ? " " + kind : ""}:${slack.brief}`;
    return isDm ? "Slack DM (summarizing…)" : "someone @you on Slack";
  }

  if (card.origin === "manual") return card.summary || "manual note";

  if (jira.status) return `Jira:${jira.status}`;
  return card.agent_state || "";
}

/** hours since a human-ball card last changed — how long it's been waiting on you. */
export function waitingHours(card: Card, now: number): number {
  if (card.ball !== "human" || !card.updated_at) return 0;
  return (now - new Date(card.updated_at).getTime()) / 3_600_000;
}

/** left-border urgency for a Need-Human card that's been waiting too long (§3B.1). */
export function agingTone(card: Card, now: number): "warn" | "urgent" | null {
  const h = waitingHours(card, now);
  if (h >= 72) return "urgent";
  if (h >= 24) return "warn";
  return null;
}

/** Need-Human ordering: act-now first. awaiting input > overdue-ish > PR to merge
 *  > everything else, then most-recently-touched. Lower = higher on the board. */
export function needHumanRank(card: Card): number {
  const c = card.cached || {};
  if (c.agent?.waiting) return 0;
  if ((c.jira?.labels || []).includes("conductor-hold")) return 1;
  if (card.origin === "github" && c.github?.my_review === "APPROVED") return 2; // ready to merge
  if (card.origin === "github") return 3;
  if (card.origin === "slack") return 4;
  return 5;
}
