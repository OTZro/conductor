// Which machine a terminal/tmux lives on — styled as a STATUS BADGE, deliberately
// unlike the zinc toolbar buttons: dot + uppercase name in a host-specific hue
// (base = cyan, remotes = violet). Same identity everywhere it appears.
export function HostChip({
  host,
  localName = "base",
}: {
  host?: string | null; // null/undefined = local
  localName?: string;
}) {
  const remote = !!host;
  return (
    <span
      title={remote ? `running on ${host} (remote via ssh)` : `running on ${localName} (this machine)`}
      className={`inline-flex items-center gap-1 text-[9px] font-semibold tracking-widest uppercase px-1.5 py-0.5 rounded ${
        remote
          ? "bg-violet-500/25 text-violet-200 border border-violet-400/50"
          : "bg-cyan-500/15 text-cyan-300 border border-cyan-500/40"
      }`}
    >
      <span className={remote ? "text-violet-400" : "text-cyan-400"}>●</span>
      {remote ? host : localName}
    </span>
  );
}

// terminal frame accent per host — the ambient signal (violet frame = remote)
export function hostFrameClass(host?: string | null): string {
  return host ? "border-violet-500/60" : "border-cyan-500/30";
}
