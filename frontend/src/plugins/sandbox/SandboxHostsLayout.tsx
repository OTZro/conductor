import { useMemo } from "react";
import type { THostSandboxes, TSandboxVM } from "./types";
import { HostChip } from "../../components/HostChip";
import type { Card } from "../../types";
import type { PluginTabProps } from "../types";

// The "sandbox-hosts" tab layout: OrbStack VMs grouped by host, one tile per VM. The
// generic PluginPanel owns the poll + outer container/header; this renders the body.
// (Formerly SandboxPanel — now a named layout the plugin framework selects.)

function uptime(s?: number | null): string {
  if (!s) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  if (d > 0) return `${d}d ${h}h`;
  return `${h}h ${Math.floor((s % 3600) / 60)}m`;
}

const STATE: Record<string, { dot: string; label: string }> = {
  running: { dot: "bg-emerald-400", label: "text-emerald-300" },
  stopped: { dot: "bg-zinc-600", label: "text-zinc-500" },
  paused: { dot: "bg-amber-400", label: "text-amber-300" },
  unknown: { dot: "bg-zinc-600", label: "text-zinc-500" },
};

// a build VM is named after its ticket (proj-9406 → PROJ-9406) — match it to the
// card for that ticket so the sandbox can jump straight to its Job.
function matchCard(vmName: string, byKey: Map<string, Card>): Card | undefined {
  const key = vmName.toUpperCase();
  return /^[A-Z][A-Z0-9]+-\d+$/.test(key) ? byKey.get(key) : undefined;
}

function Metric({ value, unit, label, title }: { value: string; unit?: string; label: string; title: string }) {
  return (
    <div title={title}>
      <div className="text-lg font-semibold text-zinc-200 tabular-nums leading-none">
        {value}
        {unit && <span className="text-[10px] text-zinc-500 ml-0.5">{unit}</span>}
      </div>
      <div className="text-[9px] text-zinc-600 uppercase tracking-wider mt-0.5">{label}</div>
    </div>
  );
}

function VmCard({ vm, card, onOpenCard }: { vm: TSandboxVM; card?: Card; onOpenCard: (id: string) => void }) {
  const st = STATE[vm.state] || STATE.unknown;
  const running = vm.state === "running";
  return (
    <div
      className={`rounded-xl border bg-zinc-900/60 p-3 space-y-2 ${
        running ? "border-zinc-700/70" : "border-zinc-800 opacity-60"
      }`}
    >
      <div className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full shrink-0 ${st.dot}`} title={vm.state} />
        {vm.link ? (
          <a
            href={vm.link}
            target="_blank"
            rel="noreferrer"
            title={vm.link}
            className="font-mono text-sm text-cyan-300 hover:underline truncate"
          >
            {vm.name} ↗
          </a>
        ) : (
          <span className="font-mono text-sm text-zinc-100 truncate">{vm.name}</span>
        )}
        <span className={`ml-auto text-[9px] uppercase tracking-wider ${st.label}`}>{vm.state}</span>
      </div>

      <div
        className="text-[10px] text-zinc-500 truncate"
        title={`${vm.distro ?? ""} ${vm.version ?? ""} ${vm.arch ?? ""}`}
      >
        {vm.distro}
        {vm.version ? ` ${vm.version}` : ""}
        {vm.arch ? ` · ${vm.arch}` : ""}
        {vm.builtin ? " · builtin" : ""}
      </div>

      <div className="flex items-end gap-5">
        <Metric
          value={vm.disk_gb != null ? String(vm.disk_gb) : "—"}
          unit={vm.disk_gb != null ? "GB" : undefined}
          label="disk"
          title="disk-image size on the host"
        />
        {vm.procs != null && <Metric value={String(vm.procs)} label="procs" title="processes inside the VM" />}
      </div>

      <div className="flex items-center gap-2 pt-1.5 border-t border-zinc-800">
        <span className="text-[10px] text-zinc-500 font-mono truncate">{vm.ip || "—"}</span>
        {card && (
          <button
            onClick={() => onOpenCard(card.id)}
            title={`open Job: ${card.title || card.external_id}`}
            className="ml-auto text-[10px] px-1.5 py-0.5 rounded border border-sky-500/40 bg-sky-500/15 text-sky-200 hover:bg-sky-500/25 shrink-0"
          >
            ▦ Job
          </button>
        )}
      </div>
    </div>
  );
}

function HostSection({
  h,
  byKey,
  onOpenCard,
}: {
  h: THostSandboxes;
  byKey: Map<string, Card>;
  onOpenCard: (id: string) => void;
}) {
  const s = h.summary;
  return (
    <div className="space-y-2.5">
      <div className="flex items-center gap-3 flex-wrap">
        <HostChip host={h.host} localName={h.name} />
        {h.online ? (
          <>
            <span className="text-sm">
              <span className="text-emerald-300 font-semibold">{s.running}</span>
              <span className="text-zinc-500"> / {s.total} running</span>
            </span>
            {s.disk_gb > 0 && (
              <span className="text-[11px] text-zinc-300" title="sum of VM disk-image sizes on this host">
                {s.disk_gb} GB VM disk
              </span>
            )}
            {h.shared.cores != null && (
              <span className="text-[11px] text-zinc-500">
                host load {h.shared.load1 ?? "—"} · {h.shared.cores} cores · up {uptime(h.shared.uptime_s)}
              </span>
            )}
          </>
        ) : (
          <span className="text-[11px] text-red-400">offline — can't reach OrbStack</span>
        )}
      </div>
      {h.online &&
        (h.vms.length ? (
          <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {h.vms.map((v) => (
              <VmCard key={v.name} vm={v} card={matchCard(v.name, byKey)} onOpenCard={onOpenCard} />
            ))}
          </div>
        ) : (
          <div className="text-[11px] text-zinc-600">no OrbStack VMs</div>
        ))}
    </div>
  );
}

export function SandboxHostsLayout({ data, cards, onOpenCard }: PluginTabProps) {
  const hosts = (data as THostSandboxes[]) || [];
  // ticket-key → card: the jira card itself wins; else any card that links that ticket.
  const byKey = useMemo(() => {
    const m = new Map<string, Card>();
    for (const c of cards) {
      if (c.origin === "jira") {
        const k = c.external_id.toUpperCase();
        if (!m.has(k)) m.set(k, c);
      }
    }
    for (const c of cards) {
      for (const l of c.links) {
        if (l.kind === "jira" && l.ref) {
          const k = l.ref.toUpperCase();
          if (!m.has(k)) m.set(k, c);
        }
      }
    }
    return m;
  }, [cards]);

  return (
    <div className="space-y-6">
      {hosts.map((h) => (
        <HostSection key={h.name} h={h} byKey={byKey} onOpenCard={onOpenCard} />
      ))}
    </div>
  );
}
