import { useEffect, useRef, useState } from "react";
import { type HostMetrics, listMetrics } from "./client";
import { HostChip } from "../../components/HostChip";

// Monitor tab: one card per machine (base cyan / remotes violet) with CPU / MEM /
// SWAP / DISK gauges, battery + uptime, session counts, and a small CPU history
// sparkline (client-side rolling window; the backend serves snapshots).

const POLL_MS = 5000;
const HISTORY = 60; // ~5 min at 5s

function gb(n?: number): string {
  return n == null ? "—" : `${(n / 2 ** 30).toFixed(1)}G`;
}

function uptime(s?: number): string {
  if (!s) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function barColor(pct: number): string {
  if (pct >= 85) return "bg-red-500/80";
  if (pct >= 60) return "bg-amber-500/80";
  return "bg-emerald-500/70";
}

function Gauge({
  label,
  pct,
  detail,
}: {
  label: string;
  pct: number;
  detail: string;
}) {
  const clamped = Math.max(0, Math.min(100, pct));
  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline justify-between text-[11px]">
        <span className="text-zinc-400 font-semibold uppercase tracking-wider">{label}</span>
        <span className="text-zinc-300 font-mono">{detail}</span>
      </div>
      <div className="h-2 rounded bg-zinc-800 overflow-hidden">
        <div
          className={`h-full rounded ${barColor(clamped)} transition-all duration-500`}
          style={{ width: `${clamped}%` }}
        />
      </div>
    </div>
  );
}

function Spark({ points, accent }: { points: number[]; accent: string }) {
  if (points.length < 2) return null;
  const w = 240;
  const h = 36;
  const max = Math.max(100, ...points);
  const step = w / (HISTORY - 1);
  const xy = points
    .map((p, i) => `${(i + (HISTORY - points.length)) * step},${h - (p / max) * h}`)
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-9" preserveAspectRatio="none">
      <polyline points={xy} fill="none" stroke={accent} strokeWidth="1.5" opacity="0.9" />
    </svg>
  );
}

function Stat({
  label,
  value,
  title,
  accent,
}: {
  label: string;
  value: number;
  title: string;
  accent?: boolean;
}) {
  return (
    <div className="flex flex-col items-center px-2" title={title}>
      <span className={`text-lg font-semibold ${accent && value > 0 ? "text-sky-300" : "text-zinc-200"}`}>
        {value}
      </span>
      <span className="text-[9px] text-zinc-500 uppercase tracking-wider">{label}</span>
    </div>
  );
}

function HostCard({ m, history }: { m: HostMetrics; history: number[] }) {
  const remote = !!m.host;
  const frame = remote ? "border-violet-500/40" : "border-cyan-500/30";
  const accent = remote ? "#a78bfa" : "#22d3ee";
  if (!m.online) {
    return (
      <div className={`rounded-xl border ${frame} bg-zinc-900/60 p-4 opacity-60 space-y-2`}>
        <div className="flex items-center gap-2">
          <HostChip host={m.host} localName={m.name} />
          <span className="text-xs text-red-400/90 font-semibold">offline</span>
        </div>
        <div className="text-xs text-zinc-500">unreachable over ssh — metrics resume when it's back</div>
      </div>
    );
  }
  const swapTotal = m.swap?.total_mb || 0;
  const swapUsed = m.swap?.used_mb || 0;
  const s = m.sessions;
  return (
    <div className={`rounded-xl border ${frame} bg-zinc-900/60 p-4 space-y-3`}>
      <div className="flex items-center gap-2 flex-wrap">
        <HostChip host={m.host} localName={m.name} />
        <span className="text-[11px] text-zinc-500">up {uptime(m.uptime_s)}</span>
        {m.battery && (
          <span
            className={`text-[11px] ${m.battery.pct <= 20 && !m.battery.ac ? "text-red-400" : "text-zinc-400"}`}
            title={m.battery.state}
          >
            {m.battery.ac ? "🔌" : "🔋"} {m.battery.pct}%
          </span>
        )}
        <span className="ml-auto text-[10px] text-zinc-600 font-mono">
          load {m.load?.map((x) => x.toFixed(1)).join(" / ")}
        </span>
      </div>

      <Spark points={history} accent={accent} />

      <div className="grid grid-cols-1 gap-2.5">
        <Gauge
          label="CPU"
          pct={m.cpu_pct || 0}
          detail={`${(m.cpu_pct || 0).toFixed(0)}% · ${m.cores} cores`}
        />
        <Gauge
          label="MEM"
          pct={m.mem?.pct || 0}
          detail={`${gb(m.mem?.used)} / ${gb(m.mem?.total)} · ${(m.mem?.pct || 0).toFixed(0)}%`}
        />
        <Gauge
          label="SWAP"
          pct={swapTotal ? (swapUsed / swapTotal) * 100 : 0}
          detail={`${(swapUsed / 1024).toFixed(1)}G / ${(swapTotal / 1024).toFixed(1)}G`}
        />
        <Gauge
          label="DISK"
          pct={m.disk?.pct || 0}
          detail={`${gb(m.disk?.used)} / ${gb(m.disk?.total)} · ${(m.disk?.pct || 0).toFixed(0)}%`}
        />
      </div>

      <div className="flex items-center justify-around pt-1 border-t border-zinc-800">
        <Stat
          label="claude"
          value={s?.claude ?? 0}
          accent
          title="total claude processes running on this machine — Conductor-opened sessions and any claude you ran by hand"
        />
        <Stat
          label="conductor"
          value={s?.conductor ?? 0}
          title="tmux sessions created by Conductor (conductor-*) — New Claude session / Resume from a card both count"
        />
        <Stat
          label="other tmux"
          value={s?.tmux_other ?? 0}
          title="other tmux sessions — ones you opened manually in a terminal (not created by Conductor)"
        />
      </div>
    </div>
  );
}

export function MonitorPanel() {
  const [metrics, setMetrics] = useState<HostMetrics[]>([]);
  const [err, setErr] = useState(false);
  const histRef = useRef<Record<string, number[]>>({});
  const [, bump] = useState(0);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await listMetrics();
        if (stop) return;
        for (const m of data) {
          if (!m.online) continue;
          const h = (histRef.current[m.name] ||= []);
          h.push(m.cpu_pct || 0);
          if (h.length > HISTORY) h.shift();
        }
        setMetrics(data);
        setErr(false);
      } catch {
        if (!stop) setErr(true);
      }
      bump((n) => n + 1);
    };
    tick();
    const t = setInterval(tick, POLL_MS);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, []);

  return (
    <div className="h-full overflow-y-auto bg-zinc-950 p-4">
      <div className="flex items-center gap-2 mb-3">
        <h2 className="text-sm font-semibold text-zinc-200">Machines</h2>
        <span className="text-[10px] text-zinc-600">refreshes every {POLL_MS / 1000}s</span>
        {err && <span className="text-[11px] text-red-400">fetch failed — retrying</span>}
      </div>
      <div className="grid gap-4 sm:grid-cols-2 max-w-4xl">
        {metrics.map((m) => (
          <HostCard key={m.name} m={m} history={histRef.current[m.name] || []} />
        ))}
        {!metrics.length && !err && (
          <div className="text-sm text-zinc-600 py-8">collecting…</div>
        )}
      </div>
    </div>
  );
}
