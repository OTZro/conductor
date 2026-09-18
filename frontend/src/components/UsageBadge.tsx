import { useEffect, useState } from "react";
import { getUsage } from "../api";
import type { Usage, UsageWindow } from "../api";
import { HoverCard } from "../ui/HoverCard";

// Claude Code usage in the header: current-session (5h) + weekly (7d) percentages.
// Hover (desktop) / tap (touch) opens the project HoverCard with each window's used%
// + reset time. Sourced from /api/usage (the statusline snapshot).
//
// The snapshot only refreshes when a Claude session renders its statusline, so a window
// that has rolled over arrives flagged `expired` with a null percentage — show "—", not
// the pre-reset number the file still carries.

function color(pct?: number | null): string {
  if (pct == null) return "text-zinc-500";
  if (pct >= 90) return "text-red-400";
  if (pct >= 70) return "text-amber-400";
  return "text-emerald-400";
}

function resetText(epoch?: number | null, expired?: boolean): string {
  if (!epoch) return "reset time unknown";
  const ms = epoch * 1000;
  const t0 = new Date(ms).toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
  if (expired) return `reset ${t0} — waiting for a session to report`;
  const diff = ms - Date.now();
  const mins = Math.round(diff / 60000);
  const rel =
    mins <= 0
      ? "any moment"
      : mins < 60
        ? `${mins}m`
        : mins < 1440
          ? `${Math.floor(mins / 60)}h ${mins % 60}m`
          : `${Math.floor(mins / 1440)}d ${Math.floor((mins % 1440) / 60)}h`;
  const t = new Date(ms).toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
  return `resets in ${rel} · ${t}`;
}

// round to ≤2 decimals and drop trailing zeros so a float like 14.000000000000002
// shows as "14%", not the raw artifact.
function pctText(w: UsageWindow): string | null {
  const pct = w?.used_percentage;
  return pct == null ? null : `${Number(pct.toFixed(2))}%`;
}

function Pill({ label, data }: { label: string; data: UsageWindow }) {
  const text = pctText(data);
  return (
    <span className="flex items-center gap-1">
      <span className="text-[10px] text-zinc-500 uppercase">{label}</span>
      <span className={`font-mono font-semibold ${color(data?.used_percentage)}`}>{text ?? "—"}</span>
    </span>
  );
}

function DetailRow({ kind, data }: { kind: string; data: UsageWindow }) {
  const text = pctText(data);
  return (
    <div className="flex flex-col">
      <span className="text-zinc-400">{kind}</span>
      <span className={color(data?.used_percentage)}>
        {data?.expired
          ? resetText(data?.resets_at, true)
          : text == null
            ? "no data yet — needs an active Claude session to report"
            : `${text} used · ${resetText(data?.resets_at)}`}
      </span>
    </div>
  );
}

export function UsageBadge() {
  const [u, setU] = useState<Usage | null>(null);

  useEffect(() => {
    let stop = false;
    const tick = () =>
      getUsage()
        .then((d) => !stop && setU(d))
        .catch(() => {});
    tick();
    const t = setInterval(tick, 60000); // usage moves slowly
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, []);

  if (!u || (!u.session && !u.weekly)) return null;
  return (
    <HoverCard
      width={260}
      content={
        <div className="space-y-2">
          <div className="font-semibold text-zinc-100">Claude Code usage</div>
          <DetailRow kind="Current 5h session" data={u.session} />
          <DetailRow kind="Weekly (7d)" data={u.weekly} />
        </div>
      }
    >
      <div className="flex items-center gap-2.5 text-xs px-2 py-1 rounded bg-zinc-800/70 border border-zinc-700">
        <span className="text-[9px] text-zinc-600">◈</span>
        <Pill label="session" data={u.session} />
        <span className="text-zinc-700">·</span>
        <Pill label="week" data={u.weekly} />
      </div>
    </HoverCard>
  );
}
