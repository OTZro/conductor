import { useEffect, useState } from "react";
import { type HostMetrics, listMetrics } from "./client";
import { barColor } from "./MonitorPanel";
import type { PluginMenuProps } from "../types";
import { HoverCard } from "../../ui/HoverCard";

// The "monitor-hosts" menu-bar widget: a compact per-host status in the header
// (dot + name + CPU%), base cyan / remote violet, red when offline. Hover (desktop)
// opens the project HoverCard with the full per-host breakdown — same bar gauges as
// the Monitor tab, just compact; click opens the Monitor tab. Reuses /api/metrics.
const POLL_MS = 10000;

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

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-[10px] uppercase tracking-wider text-zinc-300">{label}</span>
      <span className="font-mono text-zinc-200">{value}</span>
    </div>
  );
}

// compact version of the Monitor tab's Gauge: label + detail on one line, bar under it
function Bar({ label, pct, detail }: { label: string; pct: number; detail: string }) {
  const clamped = Math.max(0, Math.min(100, pct));
  return (
    <div className="space-y-0.5">
      <div className="flex justify-between gap-4">
        <span className="text-[10px] uppercase tracking-wider text-zinc-300">{label}</span>
        <span className="font-mono text-zinc-200">{detail}</span>
      </div>
      <div className="h-1.5 rounded bg-zinc-800 overflow-hidden">
        <div className={`h-full rounded ${barColor(clamped)}`} style={{ width: `${clamped}%` }} />
      </div>
    </div>
  );
}

function HostDetail({ m }: { m: HostMetrics }) {
  if (!m.online) {
    return (
      <div>
        <div className="font-semibold text-zinc-100">{m.name}</div>
        <div className="text-red-400/90">offline — unreachable over ssh</div>
      </div>
    );
  }
  const s = m.sessions;
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className={`font-semibold ${m.host ? "text-violet-300" : "text-cyan-300"}`}>{m.name}</span>
        <span className="text-[10px] text-zinc-400">up {uptime(m.uptime_s)}</span>
      </div>
      <Bar label="CPU" pct={m.cpu_pct || 0} detail={`${(m.cpu_pct || 0).toFixed(0)}% · ${m.cores ?? "?"} cores`} />
      {m.mem && <Bar label="Mem" pct={m.mem.pct} detail={`${gb(m.mem.used)} / ${gb(m.mem.total)} · ${m.mem.pct.toFixed(0)}%`} />}
      {m.disk && <Bar label="Disk" pct={m.disk.pct} detail={`${gb(m.disk.used)} / ${gb(m.disk.total)} · ${m.disk.pct.toFixed(0)}%`} />}
      {m.load && <Row label="Load" value={m.load.map((x) => x.toFixed(2)).join(" / ")} />}
      {m.battery && <Row label="Battery" value={`${m.battery.ac ? "🔌" : "🔋"} ${m.battery.pct}%`} />}
      {s && (
        <div className="mt-1 pt-1 border-t border-zinc-800">
          <Row label="claude" value={String(s.claude)} />
          <Row label="conductor" value={String(s.conductor)} />
          <Row label="other tmux" value={String(s.tmux_other)} />
        </div>
      )}
    </div>
  );
}

export function MonitorMenuBar({ onOpenView }: PluginMenuProps) {
  const [metrics, setMetrics] = useState<HostMetrics[]>([]);

  useEffect(() => {
    let stop = false;
    const tick = () =>
      listMetrics()
        .then((d) => {
          if (!stop) setMetrics(d);
        })
        .catch(() => {});
    tick();
    const t = setInterval(tick, POLL_MS);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, []);

  if (!metrics.length) return null;
  return (
    <HoverCard
      hoverOnly
      width={240}
      content={
        <div className="space-y-2.5">
          {metrics.map((m) => (
            <HostDetail key={m.name} m={m} />
          ))}
          <div className="border-t border-zinc-800 pt-1 text-[10px] text-zinc-400">click to open Monitor</div>
        </div>
      }
    >
      <button
        onClick={() => onOpenView("monitor")}
        className="flex items-center gap-2.5 px-1.5 py-0.5 rounded hover:bg-zinc-800"
      >
        {metrics.map((m) => (
          <span key={m.name} className="flex items-center gap-1">
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                !m.online ? "bg-red-500" : m.host ? "bg-violet-400" : "bg-cyan-400"
              }`}
            />
            <span className="text-zinc-400">{m.name}</span>
            {m.online && <span className="text-zinc-500 tabular-nums">{Math.round(m.cpu_pct || 0)}%</span>}
          </span>
        ))}
      </button>
    </HoverCard>
  );
}
