import { useCallback, useEffect, useState } from "react";
import { Btn, CardSkeleton, Chip } from "../../ui/primitives";
import { MarketplaceDialog, type DialogState } from "./ConfirmDialog";
import { RepoLink, Spinner } from "./ui";

// The marketplace tab. Self-contained: drives /api/plugins/marketplace/* itself, same
// contract as the plugin manager panel next to it — nothing here takes effect live
// (backend plugins load at import, frontend layouts bundle at build), so every mutation
// just stages a change and the pending banner owns the one verb that makes it real:
// Apply (rebuild + restart). Every install/update/remove/apply confirmation runs
// through MarketplaceDialog — no window.confirm/alert anywhere in this plugin.
//
// Copy is HACS-terse and English, matching the app's core chrome (Board's lane names,
// the nav, the updater menu-bar widget's "Update now") — not the Plugin Manager
// panel's Chinese, which belongs to that one surface, not the app as a whole.

type IndexEntry = {
  name: string | null; // null for a custom repo whose conductor-plugin.json didn't resolve
  repo: string;
  description: string;
  tags: string[];
  version?: string | null; // resolved from the repo's manifest — custom entries only
  manifest_unavailable?: boolean; // custom entry whose manifest couldn't be read
  name_conflict?: boolean; // custom entry whose resolved name collides with another entry's (different repo)
  source: "index" | "custom";
  installed: boolean;
  installed_name: string | null;
  installed_version: string | null;
};

type InstalledRow = {
  name: string;
  repo: string;
  ref: string;
  version: string;
  installed_at: string;
  has_backend: boolean;
  has_frontend: boolean;
  current_version?: string | null;
  latest_version?: string | null;
  update_available?: boolean;
  error?: boolean;
  // STAGED (this row's `version`, on disk) vs RUNNING (what this backend process has
  // actually loaded) can disagree until Apply — pending_apply is that split. While
  // true, "update available" is forced false server-side: the actionable next step
  // is Apply, not another Update on top of one that hasn't taken effect yet.
  pending_apply?: boolean;
};

type Pending = { name: string; action: "install" | "update" | "remove"; at: string; rebuild: boolean };

const ACTION_LABEL: Record<Pending["action"], string> = {
  install: "Install", update: "Update", remove: "Remove",
};

async function api<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api/plugins/marketplace${path}`, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => null);
    throw new Error((d && d.detail) || `HTTP ${r.status}`);
  }
  return r.json() as Promise<T>;
}

function SourceChip({ source }: { source: "index" | "custom" }) {
  return source === "custom"
    ? <Chip tone="neutral">Custom</Chip>
    : <Chip tone="neutral">Index</Chip>;
}

// Repo link inside a row: truncates with an ellipsis instead of forcing the row to
// wrap — the action buttons live in their own shrink-0 group (below) so a long URL
// can never push them onto their own line.
const ROW_REPO_LINK_CLASS = "inline-block align-bottom truncate max-w-[220px] sm:max-w-[360px]";

function BrowseRow({ e, busy, onInstall }: { e: IndexEntry; busy: boolean; onInstall: (e: IndexEntry) => void }) {
  return (
    <div className="rounded border border-zinc-800 bg-surface-raised px-3 py-2.5">
      <div className="flex items-center gap-2">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          <span className="text-body font-medium text-zinc-100">{e.name || e.repo}</span>
          {e.name && <RepoLink repo={e.repo} className={ROW_REPO_LINK_CLASS} />}
          <SourceChip source={e.source} />
          {e.name_conflict && <Chip tone="warn">Name conflict</Chip>}
          {!e.installed && e.version && <Chip tone="neutral">{e.version}</Chip>}
          {e.tags.map((t) => <Chip key={t} tone="neutral">{t}</Chip>)}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {e.installed ? (
            <Chip tone="ok" title={e.installed_name ?? undefined}>Installed {e.installed_version}</Chip>
          ) : (
            <Btn variant="primary" onClick={() => onInstall(e)} disabled={busy}>Install</Btn>
          )}
        </div>
      </div>
      {e.description && <p className="mt-1.5 text-body-s leading-relaxed text-zinc-400">{e.description}</p>}
      {e.manifest_unavailable && <p className="mt-1.5 text-body-s text-zinc-500">Manifest unavailable.</p>}
    </div>
  );
}

function InstalledRowView({ r, busy, onUpdate, onRemove }: {
  r: InstalledRow; busy: boolean;
  onUpdate: (r: InstalledRow) => void; onRemove: (r: InstalledRow) => void;
}) {
  return (
    <div className="rounded border border-zinc-800 bg-surface-raised px-3 py-2.5">
      <div className="flex items-center gap-2">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          <span className="text-body font-medium text-zinc-100">{r.name}</span>
          <RepoLink repo={r.repo} className={ROW_REPO_LINK_CLASS} />
          <Chip tone="neutral">{r.version}</Chip>
          {r.has_backend && <Chip tone="neutral">BE</Chip>}
          {r.has_frontend && <Chip tone="neutral">FE</Chip>}
          {r.error && <Chip tone="warn">Unreachable</Chip>}
          {r.pending_apply ? (
            <Chip tone="warn">Pending apply</Chip>
          ) : (
            r.update_available && <Chip tone="ok">Update available</Chip>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {r.pending_apply ? (
            <Btn disabled title="Staged — use Apply above to load it">Pending</Btn>
          ) : (
            <Btn variant={r.update_available ? "primary" : "ghost"} onClick={() => onUpdate(r)} disabled={busy}>
              Update
            </Btn>
          )}
          <Btn onClick={() => onRemove(r)} disabled={busy}>Remove</Btn>
        </div>
      </div>
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="space-y-2">
      <CardSkeleton />
      <CardSkeleton />
      <CardSkeleton />
    </div>
  );
}

export function MarketplacePanel() {
  const [tab, setTab] = useState<"browse" | "installed">("browse");
  const [entries, setEntries] = useState<IndexEntry[] | null>(null);
  const [installed, setInstalled] = useState<InstalledRow[] | null>(null);
  const [pending, setPending] = useState<Pending[]>([]);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const [customUrl, setCustomUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null); // which button/action is in flight
  const [restarting, setRestarting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [dialogBusy, setDialogBusy] = useState(false);

  const loadIndex = useCallback(async () => {
    const d = await api<{ plugins: IndexEntry[] }>("/index");
    setEntries(d.plugins);
  }, []);

  const loadInstalled = useCallback(async (force = false) => {
    const d = await api<{ plugins: InstalledRow[]; checked_at: string; pending: Pending[] }>(
      `/installed${force ? "?force=true" : ""}`,
    );
    setInstalled(d.plugins);
    setPending(d.pending);
    setCheckedAt(d.checked_at);
  }, []);

  useEffect(() => {
    Promise.all([loadIndex(), loadInstalled()]).catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, [loadIndex, loadInstalled]);

  const run = async (fn: () => Promise<unknown>, note?: string) => {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      await fn();
      if (note) setMsg(note);
      await Promise.all([loadIndex(), loadInstalled()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setPendingAction(null);
    }
  };

  const openInstall = (e: IndexEntry) =>
    setDialog({ kind: "install", repo: e.repo, name: e.name, description: e.description, tags: e.tags });
  const openUpdate = (r: InstalledRow) =>
    setDialog({ kind: "update", name: r.name, repo: r.repo, pendingApply: r.pending_apply ?? false });
  const openRemove = (r: InstalledRow) => setDialog({ kind: "remove", name: r.name, repo: r.repo });
  const openApply = () =>
    setDialog({ kind: "apply", pending, rebuild: pending.some((p) => p.rebuild) });

  const confirmDialog = async () => {
    if (!dialog) return;
    setDialogBusy(true);
    setErr(null);
    setMsg(null);
    try {
      if (dialog.kind === "install") {
        await api("/install", { repo: dialog.repo });
        setMsg(`Installed ${dialog.name || dialog.repo}.`);
      } else if (dialog.kind === "update") {
        await api("/update", { name: dialog.name });
        setMsg(`Updated ${dialog.name}.`);
      } else if (dialog.kind === "remove") {
        await api("/remove", { name: dialog.name });
        setMsg(`Removed ${dialog.name}.`);
      } else {
        // A process-generation id (random per boot, see backend/conductor/main.py)
        // is the race-free signal: an "observed down period" heuristic can miss a
        // restart fast enough to complete entirely between two 3s polls (backend-
        // only applies with no rebuild step routinely do), reporting "timed out"
        // even though it worked. Comparing generations can't have that gap — a new
        // process always has a new one, however fast the restart was.
        let beforeGeneration: string | null = null;
        try {
          const h = await fetch("/api/health");
          beforeGeneration = h.ok ? ((await h.json()).generation ?? null) : null;
        } catch {
          /* couldn't read a baseline — fall back to "any healthy response" below */
        }
        await api("/apply", {});
        setDialog(null);
        setDialogBusy(false);
        setRestarting(true);
        const t0 = Date.now();
        while (Date.now() - t0 < 180_000) {
          await new Promise((r) => setTimeout(r, 3000));
          try {
            const h = await fetch("/api/health");
            if (h.ok) {
              const generation = (await h.json()).generation ?? null;
              if (beforeGeneration === null || generation !== beforeGeneration) {
                location.reload();
                return;
              }
            }
          } catch {
            /* still down — keep waiting */
          }
        }
        setRestarting(false);
        setErr("Restart timed out. Check marketplace-apply.log.");
        return;
      }
      setDialog(null);
      await Promise.all([loadIndex(), loadInstalled()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setDialog(null);
    } finally {
      setDialogBusy(false);
    }
  };

  const addCustomRepo = () => {
    const url = customUrl.trim();
    if (!url) return;
    setPendingAction("addrepo");
    run(async () => {
      await api("/custom_repo", { url });
      setCustomUrl("");
    }, "Added.");
  };

  const recheckUpdates = () => {
    setPendingAction("checkupdates");
    run(() => loadInstalled(true));
  };

  if (restarting) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-body text-zinc-400">
        <Spinner className="h-6 w-6" />
        Restarting…
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto bg-zinc-950">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 p-4">
        {pending.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 rounded border border-sev-warn/40 bg-sev-warn/10 px-3 py-2">
            <div className="flex flex-wrap items-center gap-1.5 text-body-s text-zinc-200">
              <span>Pending:</span>
              {pending.map((p) => (
                <Chip key={p.name} tone="warn">{p.name} · {ACTION_LABEL[p.action]}</Chip>
              ))}
            </div>
            <span className="flex-1" />
            <Btn variant="primary" onClick={openApply} disabled={busy}>Apply</Btn>
          </div>
        )}

        <div className="flex items-center gap-1 border-b border-zinc-800">
          {(["browse", "installed"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-3 py-2 text-body-s font-medium transition-colors ${
                tab === t ? "border-b-2 border-zinc-100 text-zinc-100" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {t === "browse" ? "Browse" : `Installed (${installed?.length ?? 0})`}
            </button>
          ))}
        </div>

        {tab === "browse" && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <input
                value={customUrl}
                onChange={(ev) => setCustomUrl(ev.target.value)}
                placeholder="owner/repo or git URL"
                className="w-80 rounded-chip border border-zinc-700 bg-surface-hover px-2 py-1.5 text-base"
                onKeyDown={(ev) => ev.key === "Enter" && addCustomRepo()}
              />
              <Btn onClick={addCustomRepo} disabled={busy || !customUrl.trim()}>
                {busy && pendingAction === "addrepo" ? <Spinner /> : "Add"}
              </Btn>
            </div>
            {entries === null ? (
              <ListSkeleton />
            ) : (
              <div className="space-y-2">
                {entries.length === 0 && <div className="text-body-s text-zinc-500">No plugins found.</div>}
                {entries.map((e) => (
                  <BrowseRow key={`${e.source}:${e.repo}`} e={e} busy={busy} onInstall={openInstall} />
                ))}
              </div>
            )}
          </>
        )}

        {tab === "installed" && (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-caption text-zinc-500">
                {checkedAt ? `Checked ${new Date(checkedAt).toLocaleString()}` : ""}
              </span>
              <span className="flex-1" />
              <Btn onClick={recheckUpdates} disabled={busy}>
                {busy && pendingAction === "checkupdates" ? <Spinner /> : "Refresh"}
              </Btn>
            </div>
            {installed === null ? (
              <ListSkeleton />
            ) : installed.length === 0 ? (
              <div className="text-body-s text-zinc-500">No plugins installed.</div>
            ) : (
              installed.map((r) => (
                <InstalledRowView key={r.name} r={r} busy={busy} onUpdate={openUpdate} onRemove={openRemove} />
              ))
            )}
          </div>
        )}

        {msg && <div className="text-body-s text-sev-ok">{msg}</div>}
        {err && <div className="text-body-s text-sev-urgent">{err}</div>}
      </div>

      {dialog && (
        <MarketplaceDialog
          state={dialog}
          busy={dialogBusy}
          onCancel={() => !dialogBusy && setDialog(null)}
          onConfirm={confirmDialog}
        />
      )}
    </div>
  );
}
