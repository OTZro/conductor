// Sandbox plugin's own FE types (were in the shared api.ts). The tab data arrives via
// the generic /api/plugins/sandbox/tab endpoint (typed `unknown`); the layout casts it.
export type TSandboxVM = {
  name: string;
  state: string; // running | stopped | paused | unknown
  distro: string | null;
  version: string | null;
  arch: string | null;
  builtin: boolean;
  disk_gb: number | null; // per-VM disk image size (reported by newer orb only)
  ip: string | null;
  procs: number | null; // process count inside a running VM
  link: string | null; // http://<name>.orb.local:<port>/ for a running VM
};
export type THostSandboxes = {
  host: string | null;
  name: string;
  online: boolean;
  vms: TSandboxVM[];
  shared: { load1: number | null; uptime_s: number | null; cores: number | null };
  summary: { total: number; running: number; stopped: number; disk_gb: number };
};
