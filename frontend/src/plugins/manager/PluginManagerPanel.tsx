import { useCallback, useEffect, useRef, useState } from "react";
import { Btn } from "../../ui/primitives";

// The plugin manager tab. Self-contained: drives /api/plugins/manage/* itself.
//
// The one design commitment here is honesty about the lifecycle: nothing this panel
// does takes effect live (backend plugins run at import, frontend layouts are baked in
// at build), so every mutation just marks the board dirty and the banner owns the only
// verb that makes anything real — restart, with a frontend rebuild when needed.

type Row = {
  module: string;
  source: "shipped" | "local";
  id: string | null;
  label: string | null;
  enabled: boolean;
  enabled_at_boot: boolean | null; // null = this boot never saw the module (fresh import)
  loaded: boolean;
  error: string | null;
  has_frontend: boolean;
  frontend_only?: boolean;
  github: string | null;
  description: string;
};

// One render slot's plugin list (from GET /manage/slots), in current effective order —
// what the "版面排序" section's per-slot sub-block renders. `enabled` here mirrors the
// switchboard's CURRENT position (not enabled_at_boot): a plugin already loaded this
// boot but since toggled off still shows here, grayed, per the redesign's requirement
// that disabled plugins stay orderable.
type SlotRow = { module: string; id: string; label: string; enabled: boolean; order: number };
type Slots = Record<string, SlotRow[]>;

const SLOT_LABELS: Record<string, string> = {
  tab: "側邊分頁",
  card_widget: "卡片區塊",
  menu_bar: "Menu bar",
};
const SLOT_ORDER = ["tab", "card_widget", "menu_bar"];

type Upd = { local_sha: string | null; remote_sha: string | null; update_available: boolean };

async function api<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api/plugins/manage${path}`, body === undefined ? {} : {
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

function Chip({ tone = "zinc", title, children }: {
  tone?: "zinc" | "warn" | "ok" | "urgent" | "ai";
  title?: string;
  children: React.ReactNode;
}) {
  const cls = {
    zinc: "bg-zinc-800 text-zinc-400",
    warn: "bg-sev-warn/15 text-sev-warn",
    ok: "bg-sev-ok/15 text-sev-ok",
    urgent: "bg-sev-urgent/15 text-sev-urgent",
    ai: "bg-state-ai/15 text-zinc-300",
  }[tone];
  return <span className={`rounded-chip px-1.5 py-0.5 text-caption ${cls}`} title={title}>{children}</span>;
}

function RowCard({ r, upd, busy, onToggle, onDelete, onUpdate }: {
  r: Row;
  upd?: Upd;
  busy: boolean;
  onToggle: (r: Row, v: boolean) => void;
  onDelete: (r: Row) => void;
  onUpdate: (r: Row) => void;
}) {
  const dirty = r.enabled_at_boot !== null && r.enabled !== r.enabled_at_boot;
  const fresh = r.enabled_at_boot === null;
  return (
    <div className={`rounded border border-zinc-800 bg-surface-raised px-3 py-2.5 ${r.enabled ? "" : "opacity-60"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-body font-medium text-zinc-100">{r.label || r.module}</span>
        <span className="font-mono text-caption text-zinc-500">{r.module}</span>
        {!r.enabled && <Chip>已停用</Chip>}
        {r.has_frontend && <Chip title="含前端 — 變更需要「重建前端並重啟」">FE</Chip>}
        {r.frontend_only && <Chip>前端-only</Chip>}
        {r.github && <Chip title={upd?.local_sha ? `安裝版本 ${upd.local_sha.slice(0, 7)}` : "來源 repo"}>{r.github}</Chip>}
        {upd?.update_available && (
          <Chip tone="ok" title={`遠端 ${upd.remote_sha?.slice(0, 7)} ≠ 本機 ${upd.local_sha?.slice(0, 7) ?? "未知"}`}>
            有新版
          </Chip>
        )}
        {r.error && <Chip tone="urgent" title={r.error}>載入失敗</Chip>}
        {fresh && <Chip tone="ai">待重啟載入</Chip>}
        {dirty && <Chip tone="warn">待重啟生效</Chip>}
        <span className="flex-1" />
        {r.source === "local" && (
          <>
            {r.github && (
              <Btn
                variant={upd?.update_available ? "primary" : "ghost"}
                onClick={() => onUpdate(r)}
                disabled={busy}
                title={`重抓 ${r.github}`}
              >
                更新
              </Btn>
            )}
            <a
              href={`/api/plugins/manage/export/${encodeURIComponent(r.module)}`}
              className="rounded-chip border border-zinc-700 bg-surface-hover px-2.5 py-1.5 text-body-s text-zinc-300 hover:text-zinc-100"
            >
              匯出
            </a>
            <Btn onClick={() => onDelete(r)} disabled={busy}>移除</Btn>
          </>
        )}
        {r.module === "manager" ? (
          <span className="text-caption text-zinc-500" title="管理面板不能關掉自己 — 關了就沒有地方再打開它">常駐</span>
        ) : r.enabled ? (
          <Btn onClick={() => onToggle(r, false)} disabled={busy}>停用</Btn>
        ) : (
          <Btn variant="primary" onClick={() => onToggle(r, true)} disabled={busy}>啟用</Btn>
        )}
      </div>
      {r.description && (
        // the plugin's own words: its conductor-plugin.json manifest, falling back to
        // the module docstring (read via ast — so even a disabled or broken plugin
        // gets to explain what enabling it would do)
        <p className="mt-1.5 whitespace-pre-line text-body-s leading-relaxed text-zinc-400">
          {r.description}
        </p>
      )}
    </div>
  );
}

export function PluginManagerPanel() {
  const [rows, setRows] = useState<Row[]>([]);
  // `slots` is the last-fetched SERVER truth; `localSlots` is the panel's own working
  // copy that drag/arrow moves mutate directly, with NO network call per move (the
  // redesign's whole point — moves are free until the user commits them). Resynced
  // from `slots` whenever a fresh fetch lands (initial load, or after any OTHER
  // mutation in this panel that re-calls load()) — which also doubles as "還原"'s
  // implementation when the user does it explicitly.
  const [slots, setSlots] = useState<Slots>({});
  const [localSlots, setLocalSlots] = useState<Slots>({});
  const [drag, setDrag] = useState<{ slot: string; from: number } | null>(null);
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null);
  const [updates, setUpdates] = useState<Record<string, Upd>>({});
  const [pending, setPending] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [repo, setRepo] = useState("");
  const [restarting, setRestarting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const [d, s] = await Promise.all([
        api<{ plugins: Row[]; pending_restart: boolean }>("/list"),
        api<{ slots: Slots }>("/slots"),
      ]);
      setRows(d.plugins);
      setPending(d.pending_restart);
      setSlots(s.slots);
      setLocalSlots(s.slots);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
    // the version check talks to GitHub, so it rides behind the list, never in front
    // of it — badges pop in when the answer lands, the table itself never waits
    api<{ updates: Record<string, Upd> }>("/check-updates")
      .then((d) => setUpdates(d.updates))
      .catch(() => {});
  }, [load]);

  // Pure local reorder — no API call. Both the ↑/↓ buttons and drag-and-drop funnel
  // through this one function so they can never disagree about how a move is applied.
  const moveLocal = (slot: string, from: number, to: number) => {
    setLocalSlots((prev) => {
      const arr = [...(prev[slot] ?? [])];
      if (to < 0 || to >= arr.length || from === to) return prev;
      const [item] = arr.splice(from, 1);
      arr.splice(to, 0, item);
      return { ...prev, [slot]: arr };
    });
  };

  const isSlotDirty = (slot: string) => {
    const server = (slots[slot] ?? []).map((r) => r.module);
    const local = (localSlots[slot] ?? []).map((r) => r.module);
    return server.length !== local.length || server.some((m, i) => m !== local[i]);
  };
  const dirtySlots = SLOT_ORDER.filter(isSlotDirty);

  const discardOrder = () => setLocalSlots(slots);

  const applyOrder = async () => {
    setBusy(true);
    setErr(null);
    try {
      const order: Record<string, string[]> = {};
      for (const slot of dirtySlots) order[slot] = (localSlots[slot] ?? []).map((r) => r.module);
      await api("/reorder", { order });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
      return;
    }
    // Order changes apply at manifest response time (no restart), but the FE caches
    // the manifest fetch for the whole session (see api.ts getPluginManifest) — a
    // reload is the lightest way for nav / menu-bar / card-widgets to pick up the new
    // order immediately, reusing applyRestart's own final step rather than inventing a
    // second cache-busting path.
    location.reload();
  };

  const run = async (fn: () => Promise<unknown>, note?: string) => {
    setBusy(true);
    setErr(null);
    setMsg(null); // a stale "已匯入" beside a fresh error reads as both at once
    try {
      await fn();
      if (note) setMsg(note);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const applyRestart = (rebuild: boolean) =>
    run(async () => {
      await api("/apply", { rebuild });
      setRestarting(true);
      // the backend is about to die; wait for it to come back, then reload so the
      // (possibly rebuilt) bundle and fresh manifest are what renders
      const t0 = Date.now();
      while (Date.now() - t0 < 180_000) {
        await new Promise((r) => setTimeout(r, 3000));
        try {
          const h = await fetch("/api/health");
          // not just "reachable": restarting takes a moment to go DOWN first, so an
          // immediate 200 may still be the OLD process. Give it one grace beat.
          if (h.ok && Date.now() - t0 > 6000) {
            location.reload();
            return;
          }
        } catch {
          /* still down — keep waiting */
        }
      }
      setRestarting(false);
      setErr("重啟後 3 分鐘內沒等到 health — 看看 ~/.conductor/logs/plugin-apply.log");
    });

  const importZip = (f: File) =>
    run(async () => {
      const b64 = await new Promise<string>((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(String(r.result).split(",", 2)[1] ?? "");
        r.onerror = () => rej(r.error);
        r.readAsDataURL(f);
      });
      try {
        await api("/import", { data_b64: b64 });
      } catch (e) {
        if (e instanceof Error && e.message.includes("already exists") && confirm("同名 plugin 已存在,覆蓋?(舊版會移到 disabled-plugins 備份)")) {
          await api("/import", { data_b64: b64, overwrite: true });
        } else {
          throw e;
        }
      }
    }, "已匯入 — 重啟後生效");

  const importGithub = (target: string, note: string) =>
    run(async () => {
      try {
        await api("/import-github", { repo: target });
      } catch (e) {
        if (e instanceof Error && e.message.includes("already exists") && confirm("同名 plugin 已存在,覆蓋?(舊版會移到 disabled-plugins 備份)")) {
          await api("/import-github", { repo: target, overwrite: true });
        } else {
          throw e;
        }
      }
      setRepo("");
      api<{ updates: Record<string, Upd> }>("/check-updates?force=true")
        .then((d) => setUpdates(d.updates))
        .catch(() => {});
    }, note);

  const shipped = rows.filter((r) => r.source === "shipped");
  const local = rows.filter((r) => r.source === "local");

  if (restarting) {
    return (
      <div className="flex h-full items-center justify-center text-body text-zinc-400">
        重啟中… 回來後會自動重新整理
      </div>
    );
  }

  const rowProps = {
    busy,
    onToggle: (row: Row, v: boolean) => run(() => api("/toggle", { module: row.module, enabled: v })),
    onDelete: (row: Row) => {
      if (confirm(`移除 ${row.module}?(會移到 ~/.conductor/disabled-plugins/ 備份,不是刪除)`))
        run(() => api("/delete", { module: row.module }), "已移除 — 重啟後消失");
    },
    onUpdate: (row: Row) =>
      run(() => api("/import-github", { repo: row.github, overwrite: true }), "已更新 — 重啟後生效"),
  };

  return (
    // a self-contained tab renders bare (PluginPanel: "owns its own header/scroll"),
    // so without this wrapper the list simply clipped at the viewport and stuck there
    <div className="h-full overflow-y-auto bg-zinc-950">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 p-4">
      {pending && (
        <div className="flex flex-wrap items-center gap-2 rounded border border-sev-warn/40 bg-sev-warn/10 px-3 py-2">
          <span className="text-body-s text-zinc-200">有變更尚未生效 — plugin 只在啟動時載入</span>
          <span className="flex-1" />
          <Btn variant="primary" onClick={() => applyRestart(true)} disabled={busy} title="含前端的變更需要重建 bundle">
            重建前端並重啟
          </Btn>
          <Btn onClick={() => applyRestart(false)} disabled={busy} title="純後端變更用這個就夠">
            僅重啟
          </Btn>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <input
          ref={fileRef}
          type="file"
          accept=".zip"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void importZip(f);
            e.target.value = "";
          }}
        />
        <Btn onClick={() => fileRef.current?.click()} disabled={busy}>
          匯入 zip…
        </Btn>
        <input
          value={repo}
          onChange={(e) => setRepo(e.target.value)}
          placeholder="owner/repo(GitHub 匯入)"
          className="w-64 rounded-chip border border-zinc-700 bg-surface-hover px-2 py-1.5 text-base"
        />
        <Btn
          onClick={() => importGithub(repo, "已從 GitHub 匯入 — 重啟後生效")}
          disabled={busy || !/^[\w.-]+\/[\w.-]+$/.test(repo)}
        >
          從 GitHub 匯入
        </Btn>
      </div>

      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">自訂(local)</h3>
        {local.length === 0 && <div className="text-body-s text-zinc-500">沒有自訂 plugin</div>}
        {local.map((r) => (
          <RowCard key={r.module} r={r} upd={updates[r.module]} {...rowProps} />
        ))}
      </section>

      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">系統(shipped)</h3>
        {shipped.map((r) => (
          <RowCard key={r.module} r={r} upd={updates[r.module]} {...rowProps} />
        ))}
      </section>

      <section className="space-y-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">版面排序</h3>

        {dirtySlots.length > 0 && (
          // Same pattern as the "有變更尚未生效" restart banner above: a warning-toned
          // strip with the explanatory hint on the left, the commit action on the
          // right. Nothing here is persisted until 套用並重新整理 is clicked — drag
          // and ↑/↓ only ever touch localSlots.
          <div className="flex flex-wrap items-center gap-2 rounded border border-sev-warn/40 bg-sev-warn/10 px-3 py-2">
            <span className="text-body-s text-zinc-200">有未套用的變更 — 排序只是本機草稿</span>
            <span className="flex-1" />
            <Btn onClick={discardOrder} disabled={busy}>還原</Btn>
            <Btn variant="primary" onClick={applyOrder} disabled={busy} title="套用排序並重新整理頁面,讓 nav / menu-bar / 卡片區塊套用新順序">
              套用並重新整理
            </Btn>
          </div>
        )}

        {SLOT_ORDER.map((slot) => {
          const slotRows = localSlots[slot] ?? [];
          return (
            <div key={slot} className="space-y-1.5">
              <h4 className="text-caption font-medium text-zinc-500">{SLOT_LABELS[slot]}</h4>
              {slotRows.length === 0 && (
                <div className="text-body-s text-zinc-600">沒有 plugin 提供這個區塊</div>
              )}
              {slotRows.map((r, i) => (
                <div
                  key={r.module}
                  draggable
                  onDragStart={(e) => {
                    setDrag({ slot, from: i });
                    e.dataTransfer.effectAllowed = "move";
                  }}
                  onDragOver={(e) => {
                    // Drag is scoped to ITS OWN slot block only — a dragOver from a
                    // different slot's row is ignored, so a row can never be dropped
                    // across blocks.
                    if (drag?.slot !== slot) return;
                    e.preventDefault();
                    if (dragOverIdx !== i) setDragOverIdx(i);
                  }}
                  onDrop={(e) => {
                    e.preventDefault();
                    if (drag?.slot === slot) moveLocal(slot, drag.from, i);
                    setDrag(null);
                    setDragOverIdx(null);
                  }}
                  onDragEnd={() => {
                    setDrag(null);
                    setDragOverIdx(null);
                  }}
                  className={`flex items-center gap-2 rounded border px-2.5 py-1.5 transition-colors ${
                    r.enabled ? "" : "opacity-60"
                  } ${
                    drag?.slot === slot && drag.from === i
                      ? "border-zinc-700 bg-surface-hover opacity-40"
                      : drag?.slot === slot && dragOverIdx === i
                        ? "border-sev-ok bg-surface-hover"
                        : "border-zinc-800 bg-surface-raised"
                  }`}
                >
                  <span className="shrink-0 cursor-grab select-none text-zinc-600" title="拖曳排序">⠿</span>
                  <span className="flex shrink-0 flex-col">
                    <Btn
                      className="px-1.5 py-0 leading-none"
                      onClick={() => moveLocal(slot, i, i - 1)}
                      disabled={busy || i === 0}
                      title="上移"
                    >
                      ↑
                    </Btn>
                    <Btn
                      className="px-1.5 py-0 leading-none"
                      onClick={() => moveLocal(slot, i, i + 1)}
                      disabled={busy || i === slotRows.length - 1}
                      title="下移"
                    >
                      ↓
                    </Btn>
                  </span>
                  <span className="text-body-s text-zinc-200">{r.label || r.module}</span>
                  <span className="font-mono text-caption text-zinc-500">{r.module}</span>
                  {!r.enabled && <Chip>已停用</Chip>}
                </div>
              ))}
            </div>
          );
        })}
      </section>

      {msg && <div className="text-body-s text-sev-ok">{msg}</div>}
      {err && <div className="text-body-s text-sev-urgent">{err}</div>}
      </div>
    </div>
  );
}
