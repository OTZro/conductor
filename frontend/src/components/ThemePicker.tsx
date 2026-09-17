import { useEffect, useRef, useState } from "react";
import { getThemes } from "../api";
import { usePersisted } from "../persist";
import {
  applyTheme,
  BUILTIN_THEMES,
  DEFAULT_THEME_ID,
  exportTheme,
  loadImported,
  parseImport,
  resolveTheme,
  saveImported,
  setThemes,
  THEME_EVENT,
  type Theme,
} from "../theme";

// Theme engine + panel, split apart so the panel can be embedded inside AvatarMenu's
// dropdown while the engine (which actually applies the palette to the page) keeps
// running regardless of whether that dropdown is open — the CSS variables it sets
// affect the whole app, not just the menu.
//
// Three sources, merged in precedence order — built-ins, then ~/.conductor/themes.json
// (GET /api/themes, the same user-owned-file pattern as termkeys/lanes), then anything
// imported here (localStorage). A later id wins, so a user CAN override "dark" itself
// rather than only adding beside it.
const SOURCE_LABEL: Record<string, string> = {
  builtin: "內建",
  file: "themes.json",
  imported: "已匯入",
};

export function useThemeEngine() {
  const [themeId, setThemeId] = usePersisted<string>("conductor.theme", DEFAULT_THEME_ID);
  const [termFollows, setTermFollows] = usePersisted<boolean>("conductor.termFollowsTheme", true);
  const [themes, setLocalThemes] = useState<Theme[]>(BUILTIN_THEMES);
  const [importing, setImporting] = useState(false);
  const [draft, setDraft] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const merge = (fileThemes: Theme[]) => {
    const byId = new Map<string, Theme>();
    for (const t of [...BUILTIN_THEMES, ...fileThemes, ...loadImported()]) byId.set(t.id, t);
    const list = [...byId.values()];
    setThemes(list); // the api layer reads the active palette from this
    setLocalThemes(list);
    return list;
  };

  useEffect(() => {
    merge(BUILTIN_THEMES.length ? [] : []); // seed synchronously so nothing renders empty
    getThemes()
      .then((f) => merge(f))
      .catch(() => merge([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const current = resolveTheme(themeId, themes);

  // apply on mount AND on change; the event is what makes embedded terminals respawn
  // with the new palette (they're iframes — CSS variables can't reach them). Dispatched
  // only when the RESOLVED palette differs, compared by content: `current` is a fresh
  // object identity every time the theme list refetches (merge() rebuilds the list), and
  // an identity-triggered dispatch respawned every open terminal on app load for a
  // palette that hadn't changed. First run is naturally skipped — nothing to differ from.
  const lastApplied = useRef<string | null>(null);
  useEffect(() => {
    applyTheme(current);
    const sig = JSON.stringify(current);
    if (lastApplied.current !== null && lastApplied.current !== sig)
      window.dispatchEvent(new CustomEvent(THEME_EVENT));
    lastApplied.current = sig;
  }, [current]);

  const pick = (id: string) => setThemeId(id);

  const doImport = () => {
    const { themes: parsed, error } = parseImport(draft);
    if (error) {
      setMsg(`⚠ ${error}`);
      return;
    }
    const kept = loadImported().filter((t) => !parsed.some((p) => p.id === t.id));
    saveImported([...kept, ...parsed]);
    const list = merge(themes.filter((t) => t.source === "file"));
    setDraft("");
    setImporting(false);
    setMsg(`✓ 匯入 ${parsed.length} 個主題`);
    if (parsed[0]) setThemeId(parsed[0].id); // preview it immediately
    void list;
  };

  const doRemove = (id: string) => {
    saveImported(loadImported().filter((t) => t.id !== id));
    merge(themes.filter((t) => t.source === "file"));
    if (themeId === id) setThemeId(DEFAULT_THEME_ID);
  };

  const doExport = async () => {
    const text = exportTheme(current);
    try {
      await navigator.clipboard.writeText(text);
      setMsg("✓ 已複製目前主題的 JSON");
    } catch {
      setDraft(text); // clipboard blocked (not https) — show it so it can be copied by hand
      setImporting(true);
      setMsg("⚠ 無法寫入剪貼簿,已填入下方");
    }
  };

  return {
    current,
    themes,
    termFollows,
    setTermFollows,
    pick,
    importing,
    setImporting,
    draft,
    setDraft,
    msg,
    doImport,
    doRemove,
    doExport,
  };
}

export type ThemeEngine = ReturnType<typeof useThemeEngine>;

/** The picker's actual UI — list, import/export, term-follows toggle. Rendered inside
 * AvatarMenu's dropdown, so it has no trigger button or absolute positioning of its own. */
export function ThemePickerPanel({ engine }: { engine: ThemeEngine }) {
  const { current, themes, termFollows, setTermFollows, pick, importing, setImporting, draft, setDraft, msg, doImport, doRemove, doExport } =
    engine;

  return (
    <div className="space-y-2">
      <div className="text-caption text-zinc-500">主題</div>
      <div className="space-y-0.5 max-h-56 overflow-y-auto">
        {themes.map((t) => (
          <div key={t.id} className="flex items-center gap-1">
            <button
              onClick={() => pick(t.id)}
              className={`flex-1 text-left px-2 py-1 rounded-chip border transition-colors duration-fast ${
                t.id === current.id
                  ? "bg-state-ai/20 border-state-ai/40 text-state-ai"
                  : "bg-surface-hover border-zinc-700 text-zinc-300 hover:text-zinc-100"
              }`}
            >
              <span className="mr-1">{t.base === "dark" ? "☾" : "☀"}</span>
              {t.label}
              <span className="ml-1 text-[9px] text-zinc-500">{SOURCE_LABEL[t.source || "builtin"]}</span>
              {t.tmux && Object.keys(t.tmux).length > 0 && (
                <span className="ml-1 text-[9px] text-emerald-400/80" title="這個主題也會套用 tmux 樣式">
                  tmux
                </span>
              )}
            </button>
            {t.source === "imported" && (
              <button
                onClick={() => doRemove(t.id)}
                title="移除這個匯入的主題"
                className="px-1 text-zinc-600 hover:text-red-400 leading-none"
              >
                ×
              </button>
            )}
          </div>
        ))}
      </div>

      <label className="flex items-center gap-2 cursor-pointer select-none pt-1 border-t border-zinc-800">
        <input
          type="checkbox"
          checked={termFollows}
          onChange={(e) => {
            setTermFollows(e.target.checked);
            window.dispatchEvent(new CustomEvent(THEME_EVENT)); // re-skin open terminals
          }}
          className="accent-violet-500"
        />
        <span>終端與 tmux 跟隨主題</span>
      </label>

      <div className="flex gap-1.5">
        <button
          onClick={() => setImporting((v) => !v)}
          className="flex-1 px-2 py-1 rounded-chip bg-surface-hover border border-zinc-700 text-zinc-300 hover:text-zinc-100"
        >
          匯入…
        </button>
        <button
          onClick={doExport}
          className="flex-1 px-2 py-1 rounded-chip bg-surface-hover border border-zinc-700 text-zinc-300 hover:text-zinc-100"
        >
          匯出目前
        </button>
      </div>

      {importing && (
        <div className="space-y-1">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={6}
            spellCheck={false}
            placeholder={'貼上主題 JSON(單一物件或陣列):\n{"id":"nord","label":"Nord","base":"dark",\n "vars":{"--z-950":"46 52 64"},\n "terminal":{"background":"#2e3440"},\n "tmux":{"status-style":"fg=#d8dee9,bg=#3b4252"}}'}
            // 16px — iOS's no-auto-zoom threshold. Costs some density in a JSON box,
            // which is the trade the platform imposes: the alternative is a page that
            // zooms as soon as you tap in to paste a theme.
            className="w-full text-base font-mono px-2 py-1 rounded bg-surface-hover border border-zinc-700 text-zinc-100 placeholder:text-zinc-600"
          />
          <button
            onClick={doImport}
            disabled={!draft.trim()}
            className="w-full px-2 py-1 rounded-chip bg-state-ai/20 border border-state-ai/40 text-state-ai disabled:opacity-40"
          >
            匯入
          </button>
          <div className="text-[10px] text-zinc-600">
            長期保存放 <span className="font-mono">~/.conductor/themes.json</span>(重啟後仍在, 且跨瀏覽器)
          </div>
        </div>
      )}

      {msg && <div className="text-[10px] text-zinc-400">{msg}</div>}
    </div>
  );
}
