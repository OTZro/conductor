import type { TSandboxVM } from "./types";
import { HostChip } from "../../components/HostChip";
import type { PluginCardProps } from "../types";

// The "sandbox-card" card-widget layout: the OrbStack VM matching this card's ticket.
// The section wrapper + title come from the generic PluginCardWidgets slot.
type SandboxCardData = { host: string | null; hostName: string; vm: TSandboxVM };

export function SandboxCardLayout({ data }: PluginCardProps) {
  const { host, hostName, vm } = data as SandboxCardData;
  return (
    <div className="flex items-center gap-2 flex-wrap rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2">
      <HostChip host={host} localName={hostName} />
      <span
        className={`w-2 h-2 rounded-full ${vm.state === "running" ? "bg-emerald-400" : "bg-zinc-600"}`}
        title={vm.state}
      />
      {vm.link ? (
        <a
          href={vm.link}
          target="_blank"
          rel="noreferrer"
          title={vm.link}
          className="font-mono text-sm text-cyan-300 hover:underline"
        >
          {vm.name} ↗
        </a>
      ) : (
        <span className="font-mono text-sm text-zinc-200">{vm.name}</span>
      )}
      <span className="text-[11px] text-zinc-500">{vm.state}</span>
      {vm.disk_gb != null && (
        <span className="text-[11px] text-zinc-400" title="disk-image size">
          {vm.disk_gb} GB
        </span>
      )}
      {vm.procs != null && <span className="text-[11px] text-zinc-500">{vm.procs} procs</span>}
      {vm.ip && <span className="text-[11px] text-zinc-500 font-mono">{vm.ip}</span>}
    </div>
  );
}
