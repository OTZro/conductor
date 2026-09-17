import { memo } from "react";
import { agingTone, statusSentence } from "../cardMeta";
import { useNow } from "../clock";
import type { Card } from "../types";
import { HoverCard } from "../ui/HoverCard";
import { Chip, type ChipTone, OFFSCREEN_SKIP, StatusDot } from "../ui/primitives";
import { SlackTooltipBody } from "./CardHoverBodies";
import { LinkChips } from "./LinkChips";

// Four fixed rows (§3B.3): identity+verdict+ball · title · status sentence · meta.
// Scanning the board you read rows 1 and 3; the rest is there when you look closer.

const ORIGIN: Record<string, { label: string; tone: ChipTone }> = {
  jira: { label: "JIRA", tone: "jira" },
  github: { label: "PR", tone: "pr" },
  slack: { label: "SLACK", tone: "slack" },
  manual: { label: "MANUAL", tone: "manual" },
};

// my latest review verdict (a bot/agent can post under my account) — see §my-review
const REVIEW: Record<string, { label: string; tone: ChipTone; title: string }> = {
  APPROVED: { label: "✓ Approved", tone: "ok", title: "you formally approved" },
  SUGGEST_APPROVE: { label: "🤖 suggests approve", tone: "ai", title: "suggests approve — you haven't formally approved" },
  CHANGES_REQUESTED: { label: "✗ Changes", tone: "urgent", title: "changes requested" },
  COMMENTED: { label: "💬 Comment", tone: "warn", title: "commented" },
};

const BALL_TONE = { human: "human", ai: "ai", none: "done" } as const;

// plugin badge convention — a plugin writes cached.badges.<plugin-id> via upsert_card
// and the board renders it here with no fetch and no per-plugin code.
type PluginBadge = { text: string; tone?: string; title?: string };
const BADGE_TONES = new Set<ChipTone>(["neutral", "ok", "warn", "urgent", "ai", "human", "done"]);
const badgeTone = (t?: string): ChipTone =>
  BADGE_TONES.has(t as ChipTone) ? (t as ChipTone) : "neutral";

function rel(ms: number | null | undefined, now: number): string {
  if (ms == null) return "";
  const d = (now - ms) / 1000;
  if (d < 60) return "now";
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  return `${Math.floor(d / 86400)}d`;
}

function dueTone(iso: string | null | undefined, now: number): ChipTone | null {
  if (!iso) return null;
  const days = Math.floor((new Date(`${iso}T00:00:00`).getTime() - now) / 86400000);
  if (days < 0) return "urgent";
  if (days <= 2) return "warn";
  return "neutral";
}

const AGE_BORDER = { warn: "border-l-2 border-l-sev-warn", urgent: "border-l-2 border-l-sev-urgent" };

// memo'd, and it earns it: a lane routinely holds hundreds of these, and the board
// re-renders on every 15s refetch plus every WS invalidation. react-query's structural
// sharing keeps the `card` object identity stable when its JSON didn't change, so an
// untouched card genuinely skips re-rendering — but ONLY if every other prop is stable
// too. That is why this takes `onSelect` (a setState fn, referentially stable) and
// builds the click handler itself: a per-card `onClick` closure minted by the parent
// changes identity every render and defeats the memo outright.
export const CardItem = memo(function CardItem({
  card,
  members,
  onSelect,
  aging: agingEnabled,
}: {
  card: Card;
  members?: Card[]; // jobs grouped under this one (representative card) → ⧉ badge
  onSelect: (id: string) => void;
  aging?: boolean; // lane registry flag; omitted → legacy need_human-only behavior
}) {
  // The shared clock (clock.tsx). memo() skips exactly the renders that would otherwise
  // refresh anything time-derived, so `now` comes from context and is REQUIRED by every
  // helper below that needs it — the subscription can't be dropped without a type error.
  const now = useNow();

  // No horizontal swipe gesture here: horizontal touch is reserved for the board's
  // left/right stage paging (§3F). Done/Snooze live in the card detail. A JS swipe
  // on the card would fight the native board scroll and page + move at once.
  // Unknown origin (a plugin-provided source) gets a generic chip from its own name —
  // not a lying MANUAL label.
  const o = ORIGIN[card.origin] || { label: card.origin.toUpperCase().slice(0, 8), tone: "manual" as ChipTone };
  const review = card.cached?.github?.my_review ? REVIEW[card.cached.github.my_review] : undefined;
  const unread = !card.local.read && card.ball === "human";
  const snoozed = !!card.local.snoozed_until && new Date(card.local.snoozed_until).getTime() > now;
  const dimmed = snoozed || card.local.dismissed;
  const aging = (agingEnabled ?? card.lane === "need_human") ? agingTone(card, now) : null;
  const sentence = statusSentence(card, now);
  const jira = card.cached?.jira || {};
  // plugin display convention — a plugin source may override how its cards present:
  // cached.display = {label?: string, ts?: ISO-8601} (the same privileges slack's
  // bespoke handling below gets, opened up as stored data; see docs/plugins.md)
  const disp = (card.cached?.display ?? {}) as { label?: string; ts?: string };
  // for slack the external_id is an opaque dm:/mention: key — show who sent it instead
  const slackFrom = card.cached?.slack?.from as string | undefined;
  const displayLabel = slackFrom || disp.label;
  // slack cards: the message time (slack.ts, epoch seconds) is what matters — the card's
  // updated_at is just the last poll (~now for every live slack card). Others use
  // display.ts (a plugin's own event time) when given, else updated_at.
  const slackTs = card.cached?.slack?.ts as string | undefined;
  const timeMs = slackTs
    ? parseFloat(slackTs) * 1000
    : disp.ts
      ? Date.parse(disp.ts)
      : card.updated_at
        ? Date.parse(card.updated_at)
        : null;

  // hover the origin tag to see the details without opening the card (§ board hover)
  // unknown origins truncate to 8 chars — carry the full name on hover so two
  // long plugin origins (customer-a / customer-b) stay distinguishable
  const originChip = (
    <Chip tone={o.tone} title={ORIGIN[card.origin] ? undefined : card.origin}>
      {o.label}
    </Chip>
  );
  const slackText = card.cached?.slack?.text as string | undefined;
  // claude is blocked on you: `choice` = a live question/permission dialog seen on the
  // pane (self-clearing, carries the actual question); `notification` = the hook's
  // message (permission asks). Prefer the pane's — it's fresher and more specific.
  const notification = (card.cached?.agent?.choice || card.cached?.agent?.notification) as
    | string
    | undefined;
  // a PR job's PR-status hover now lives on its PR chip (LinkChips), same as a jira card's
  // PR link — so the origin chip stays plain; only slack keeps its sender/message hover.
  const originNode =
    card.origin === "slack" && (slackFrom || slackText) ? (
      <HoverCard
        href={card.url ?? undefined}
        openLabel="open in Slack ↗"
        content={<SlackTooltipBody from={slackFrom} text={slackText} />}
      >
        {originChip}
      </HoverCard>
    ) : (
      originChip
    );

  return (
    <button
      onClick={() => onSelect(card.id)}
      // OFFSCREEN_SKIP carries the containment invariant — see ui/primitives.tsx
      className={`w-full text-left bg-surface-raised/70 hover:bg-surface-hover border border-zinc-700/60 rounded-card p-3 space-y-1.5 transition-colors duration-fast ${OFFSCREEN_SKIP} ${
        aging ? AGE_BORDER[aging] : ""
      } ${dimmed ? "opacity-60" : ""}`}
    >
      {/* row 1 — identity + verdict + ball */}
      <div className="flex items-center gap-2">
        {originNode}
        {displayLabel ? (
          <span className="text-body-s text-zinc-300 truncate" title={card.external_id}>
            {displayLabel}
          </span>
        ) : (
          <span className="text-body-s text-zinc-400 font-mono truncate">{card.external_id}</span>
        )}
        {(card.local.pinned || jira.pinned) && <span title="pinned">📌</span>}
        {members && members.length > 0 && (
          <HoverCard
            content={
              <div className="text-zinc-200">
                {`${members.length} job(s) grouped under this card${
                  members.some((m) => m.ball === "human") ? " · some need you" : ""
                }`}
              </div>
            }
          >
            <span
              className={`text-[10px] px-1 rounded leading-tight ${
                members.some((m) => m.ball === "human")
                  ? "bg-amber-500/20 text-amber-300"
                  : "bg-zinc-700 text-zinc-300"
              }`}
            >
              ⧉ {members.length}
            </span>
          </HoverCard>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          {review && (
            <HoverCard content={<div className="text-zinc-200">{review.title}</div>}>
              <Chip tone={review.tone}>{review.label}</Chip>
            </HoverCard>
          )}
          <HoverCard
            content={
              <div className="text-zinc-200">
                {unread ? `unread · ball: ${card.ball}` : `ball: ${card.ball}`}
              </div>
            }
          >
            <StatusDot
              tone={BALL_TONE[card.ball]}
              className={unread ? "ring-2 ring-offset-1 ring-offset-surface-raised ring-state-human/70" : ""}
            />
          </HoverCard>
        </div>
      </div>

      {/* row 2 — what */}
      <div className="text-body text-zinc-100 line-clamp-2">{card.title || card.external_id}</div>

      {/* row 3 — why it's in this lane (the scan line) */}
      {sentence && <div className="text-body-s text-zinc-400 line-clamp-2">{sentence}</div>}

      {/* claude is blocked waiting on you — surface its own message verbatim */}
      {notification && (
        <div className="flex items-start gap-1 text-body-s text-amber-200 bg-amber-500/10 border border-amber-500/25 rounded px-2 py-1">
          <span className="shrink-0">🔔</span>
          <span className="line-clamp-3">{notification}</span>
        </div>
      )}

      {/* row 4 — dates + links + time */}
      <div className="flex items-center gap-1.5 flex-wrap text-caption">
        {jira.due_date && (
          <Chip tone={dueTone(jira.due_date, now) || "neutral"} title="Due date(hard)">
            🚩 {jira.due_date}
          </Chip>
        )}
        {jira.target_release_date && (
          <Chip tone="neutral" title="Target Release Date">
            🎯 {jira.target_release_date}
          </Chip>
        )}
        <LinkChips card={card} />
        {/* plugin badges — declarative from stored data (cached.badges.<plugin> =
            {text, tone?, title?}), written via upsert_card; zero fetch, null clears */}
        {Object.entries((card.cached?.badges ?? {}) as Record<string, PluginBadge | null>).map(
          ([pid, b]) =>
            b?.text ? (
              <HoverCard key={pid} content={<div className="text-zinc-200">{b.title || pid}</div>}>
                <Chip tone={badgeTone(b.tone)}>{b.text}</Chip>
              </HoverCard>
            ) : null,
        )}
        {card.cached?.github?.author && (
          <HoverCard content={<div className="text-zinc-200">PR author: {card.cached.github.author}</div>}>
            <span className="text-zinc-500">👤 {card.cached.github.author}</span>
          </HoverCard>
        )}
        <span className="ml-auto flex items-center gap-1.5 text-zinc-500">
          {snoozed && <span className="text-host-remote">💤</span>}
          {card.local.dismissed && <span>✕</span>}
          {timeMs ? (
            <HoverCard content={<div className="text-zinc-200">{new Date(timeMs).toLocaleString()}</div>}>
              <span className="tabular-nums">{rel(timeMs, now)}</span>
            </HoverCard>
          ) : (
            <span className="tabular-nums">{rel(timeMs, now)}</span>
          )}
        </span>
      </div>
    </button>
  );
});
