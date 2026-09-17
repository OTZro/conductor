// Monitor plugin's own metrics client (was in the shared api.ts). Talks to this
// plugin's own /api/metrics router, mounted by the backend plugin loader.
export type HostMetrics = {
  host: string | null;
  name: string;
  online: boolean;
  cores?: number;
  load?: [number, number, number];
  cpu_pct?: number;
  mem?: { total: number; used: number; pct: number };
  swap?: { total_mb: number; used_mb: number };
  disk?: { total: number; used: number; pct: number; mount: string };
  uptime_s?: number;
  battery?: { pct: number; state: string; ac: boolean } | null;
  sessions?: { claude: number; conductor: number; tmux_other: number };
};
export const listMetrics = (): Promise<HostMetrics[]> =>
  fetch("/api/metrics").then((r) => {
    if (!r.ok) throw new Error(`metrics ${r.status}`);
    return r.json();
  });
