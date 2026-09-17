import { useEffect, useState } from "react";
import { Btn } from "../../ui/primitives";
import { Markdown } from "../../components/Markdown";
import { RepoLink, Spinner } from "./ui";

// In-app replacement for window.confirm/window.alert across the marketplace plugin —
// same overlay chrome as CreateCardModal / FilePreview (bg-black/50 backdrop, centered
// zinc-900 bordered panel, Escape + backdrop-click both cancel). One component, four
// variants: only "install" fetches + renders a README (via the app's own dependency-
// free Markdown renderer — no new library, no dangerouslySetInnerHTML); the rest are a
// plain text body. Copy is deliberately terse (HACS's register: short verbs, one-line
// warnings) and English — matching the app's core chrome (Board, nav, the updater
// widget), not the Plugin Manager panel's Chinese, which is that ONE surface's own
// convention, not the app's.

export type DialogState =
  | { kind: "install"; repo: string; name: string | null; description: string; tags: string[] }
  | { kind: "update"; name: string; repo: string; pendingApply: boolean }
  | { kind: "remove"; name: string; repo: string }
  | { kind: "apply"; pending: { name: string; action: string }[]; rebuild: boolean };

const TRUST_WARNING = "This plugin is not part of Conductor and is installed at your own risk.";

const ACTION_LABEL: Record<string, string> = { install: "Install", update: "Update", remove: "Remove" };

type ReadmeState = { status: "loading" } | { status: "ready"; text: string | null } | { status: "error" };

function useReadme(repo: string | null, name: string | null): ReadmeState {
  const [state, setState] = useState<ReadmeState>({ status: "loading" });
  useEffect(() => {
    if (!repo) return;
    let cancelled = false;
    setState({ status: "loading" });
    fetch("/api/plugins/marketplace/readme", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, ...(name ? { name } : {}) }),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<{ markdown: string | null }>;
      })
      .then((d) => !cancelled && setState({ status: "ready", text: d.markdown }))
      .catch(() => !cancelled && setState({ status: "error" }));
    return () => {
      cancelled = true;
    };
  }, [repo, name]);
  return state;
}

type UpdateCheckState =
  | { status: "skipped" } // pending-apply — no point checking, the actionable step is Apply
  | { status: "loading" }
  | { status: "ready"; current: string | null; latest: string | null; available: boolean }
  | { status: "error" };

// Opening the Update dialog always re-checks THIS one repo fresh — bypassing the
// list-wide 10-min cache — so a tag pushed moments ago is reflected immediately
// instead of the dialog trusting whatever the last list poll happened to see.
function useUpdateCheck(name: string, skip: boolean): UpdateCheckState {
  const [state, setState] = useState<UpdateCheckState>(skip ? { status: "skipped" } : { status: "loading" });
  useEffect(() => {
    if (skip) {
      setState({ status: "skipped" });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    fetch("/api/plugins/marketplace/check_update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<{ current_version: string | null; latest_version: string | null; update_available: boolean }>;
      })
      .then(
        (d) =>
          !cancelled &&
          setState({ status: "ready", current: d.current_version, latest: d.latest_version, available: d.update_available }),
      )
      .catch(() => !cancelled && setState({ status: "error" }));
    return () => {
      cancelled = true;
    };
  }, [name, skip]);
  return state;
}

function Header({ title, onCancel }: { title: string; onCancel: () => void }) {
  return (
    <div className="flex items-center gap-2 px-4 py-3 border-b border-zinc-800 shrink-0">
      <h2 className="text-body font-semibold text-zinc-100 truncate flex-1">{title}</h2>
      <button
        onClick={onCancel}
        className="text-zinc-400 hover:text-zinc-100 px-1.5 py-0.5 rounded"
        title="Close"
        aria-label="Close"
      >
        ✕
      </button>
    </div>
  );
}

function Warning({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded border border-sev-warn/40 bg-sev-warn/10 px-3 py-2 text-body-s text-zinc-200">
      {children}
    </div>
  );
}

function InstallBody({ state }: { state: Extract<DialogState, { kind: "install" }> }) {
  const readme = useReadme(state.repo, state.name);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-body font-medium text-zinc-100">{state.name || state.repo}</span>
        <RepoLink repo={state.repo} />
      </div>
      {state.description && <p className="text-body-s text-zinc-400">{state.description}</p>}
      {state.tags.length > 0 && (
        <div className="flex flex-wrap gap-1 text-caption text-zinc-500">{state.tags.join(" · ")}</div>
      )}
      <div className="rounded border border-zinc-800 bg-zinc-950 p-3 max-h-72 overflow-y-auto">
        {readme.status === "loading" && (
          <div className="flex items-center gap-2 text-body-s text-zinc-500">
            <Spinner /> Loading…
          </div>
        )}
        {readme.status === "error" && <div className="text-body-s text-sev-urgent">Couldn't load README.</div>}
        {readme.status === "ready" && readme.text === null && (
          <div className="text-body-s text-zinc-500">README unavailable.</div>
        )}
        {readme.status === "ready" && readme.text !== null && (
          <div className="text-body-s text-zinc-300">
            <Markdown text={readme.text} />
          </div>
        )}
      </div>
      <Warning>{TRUST_WARNING}</Warning>
    </div>
  );
}

function UpdateBody({ state, check }: { state: Extract<DialogState, { kind: "update" }>; check: UpdateCheckState }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-body font-medium text-zinc-100">{state.name}</span>
        <RepoLink repo={state.repo} />
      </div>
      {state.pendingApply ? (
        <p className="text-body-s text-zinc-300">
          Staged and pending apply. Restart to load it — nothing to update yet.
        </p>
      ) : (
        <>
          {check.status === "loading" && (
            <div className="flex items-center gap-2 text-body-s text-zinc-500">
              <Spinner /> Checking…
            </div>
          )}
          {check.status === "error" && <p className="text-body-s text-sev-urgent">Couldn't check for updates.</p>}
          {check.status === "ready" && (
            <p className="text-body-s text-zinc-300">
              {check.available ? `${check.current} → ${check.latest}` : check.current ?? check.latest ?? "dev"}
            </p>
          )}
          <Warning>{TRUST_WARNING}</Warning>
        </>
      )}
    </div>
  );
}

function RemoveBody({ state }: { state: Extract<DialogState, { kind: "remove" }> }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-body font-medium text-zinc-100">{state.name}</span>
        <RepoLink repo={state.repo} />
      </div>
      <p className="text-body-s text-zinc-400">The git cache is kept for a faster reinstall.</p>
    </div>
  );
}

function ApplyBody({ state }: { state: Extract<DialogState, { kind: "apply" }> }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {state.pending.map((p) => (
          <span key={p.name} className="rounded-chip bg-surface-hover px-1.5 py-0.5 text-caption text-zinc-300">
            {p.name} · {ACTION_LABEL[p.action] ?? p.action}
          </span>
        ))}
      </div>
      <p className="text-body-s text-zinc-400">
        {state.rebuild ? "Rebuilds the frontend, then restarts." : "Restarts the backend."}
      </p>
      <Warning>{TRUST_WARNING}</Warning>
    </div>
  );
}

export function MarketplaceDialog({
  state, busy, onCancel, onConfirm,
}: {
  state: DialogState;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel, busy]);

  const updateCheck = useUpdateCheck(
    state.kind === "update" ? state.name : "",
    state.kind !== "update" || state.pendingApply,
  );

  const title =
    state.kind === "install" ? `Install ${state.name || state.repo}`
    : state.kind === "update" ? `Update ${state.name}`
    : state.kind === "remove" ? `Remove ${state.name}`
    : "Apply changes";

  // Reinstall, not Update, when the fresh check finds the repo already at the
  // installed version — same /update code path either way, just an honest label.
  // Never rendered as "vX → vX" with an "Update" button.
  const confirmLabel =
    state.kind === "install" ? "Install"
    : state.kind === "update" ? (updateCheck.status === "ready" && !updateCheck.available ? "Reinstall" : "Update")
    : state.kind === "remove" ? "Remove"
    : "Apply";
  const variant = state.kind === "remove" ? "danger" : "primary";
  // A pending-apply plugin offers nothing to confirm here — Apply (the banner above)
  // is the only actionable next step, so the dialog is Cancel/Close-only.
  const showConfirm = !(state.kind === "update" && state.pendingApply);
  const confirmDisabled = busy || (state.kind === "update" && updateCheck.status === "loading");

  return (
    <div
      className="fixed inset-0 z-[70] bg-black/50 flex items-center justify-center p-4"
      onClick={() => !busy && onCancel()}
    >
      <div
        className="w-[520px] max-w-[92vw] max-h-[85vh] bg-zinc-900 border border-zinc-700 rounded-xl shadow-2xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <Header title={title} onCancel={onCancel} />
        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          {state.kind === "install" && <InstallBody state={state} />}
          {state.kind === "update" && <UpdateBody state={state} check={updateCheck} />}
          {state.kind === "remove" && <RemoveBody state={state} />}
          {state.kind === "apply" && <ApplyBody state={state} />}
        </div>
        <div className="flex justify-end gap-2 px-4 py-3 border-t border-zinc-800 shrink-0">
          <Btn onClick={onCancel} disabled={busy}>{showConfirm ? "Cancel" : "Close"}</Btn>
          {showConfirm && (
            <Btn variant={variant} onClick={onConfirm} disabled={confirmDisabled}>
              {busy ? <Spinner /> : confirmLabel}
            </Btn>
          )}
        </div>
      </div>
    </div>
  );
}
