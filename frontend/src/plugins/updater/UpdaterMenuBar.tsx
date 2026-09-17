import { useEffect, useRef, useState } from "react";

// The "updater" header widget: a badge that lights amber when the Conductor checkout is
// behind its upstream, and a popover listing the incoming commits + an "Update now" that
// fast-forwards and restarts the app in place. Talks to the updater plugin's own
// /api/update/* router. Self-contained — no props used (it has no tab to open).

type Commit = { sha: string; subject: string };
type Status = {
  current: string | null;
  branch: string | null;
  behind: number;
  commits: Commit[];
  dirty: boolean;
  has_upstream: boolean;
  updatable: boolean;
  blocked_reason: string | null;
  fetch_error: boolean; // last fetch couldn't reach the remote — the status below is stale
  git_error: boolean; // a git command failed/timed out — the status below is unknown, not clean
  checked_at: string | null;
};

const j = (r: Response) => r.json();
const getStatus = (): Promise<Status> => fetch("/api/update/status").then(j);
const postCheck = (): Promise<Status> => fetch("/api/update/check", { method: "POST" }).then(j);
const postApply = (): Promise<{ started: boolean; reason?: string; from?: string }> =>
  fetch("/api/update/apply", { method: "POST" }).then(j);

const POLL_MS = 60_000; // passive re-check of the cached status (no network on the backend)
const APPLY_POLL_MS = 2_000; // while the backend restarts, watch for the new version
const APPLY_TIMEOUT_MS = 180_000;

type Phase = "idle" | "applying" | "done" | "error";

function ago(iso: string | null): string {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export function UpdaterMenuBar() {
  const [st, setSt] = useState<Status | null>(null);
  const [open, setOpen] = useState(false);
  const [checking, setChecking] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [refusal, setRefusal] = useState<string | null>(null); // why the backend refused an apply
  const fromShaRef = useRef<string | null>(null);
  const applyTimerRef = useRef<ReturnType<typeof setInterval>>();
  const applyStartRef = useRef(0);

  useEffect(() => {
    let alive = true;
    getStatus().then((s) => alive && setSt(s)).catch(() => {});
    const id = setInterval(() => {
      if (phase === "applying") return; // the apply poll owns the fetch during a restart
      getStatus().then((s) => alive && setSt(s)).catch(() => {});
    }, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [phase]);

  useEffect(() => () => clearInterval(applyTimerRef.current), []);

  const check = () => {
    setChecking(true);
    postCheck()
      .then((s) => {
        setSt(s);
        // a transient failure may have parked us in "error"/"done"; a fresh successful
        // check clears it so the update action isn't hidden behind a stale phase.
        setPhase("idle");
        setRefusal(null);
      })
      .catch(() => {})
      .finally(() => setChecking(false));
  };

  const apply = async () => {
    fromShaRef.current = st?.current ?? null;
    setRefusal(null);
    setPhase("applying"); // claim in-flight BEFORE the request so a double-click can't fire two
    let res: { started: boolean; reason?: string };
    try {
      res = await postApply();
    } catch {
      setPhase("error");
      return;
    }
    if (!res.started) {
      setPhase("idle"); // guard rejected it (dirty / diverged / already current) — re-arm
      // Keep the reason: without it the popover just drops back to idle and the user clicks
      // "Update now" again with no idea why nothing happened. Refresh the status inline
      // rather than via check(), which would clear the refusal we just set.
      setRefusal(res.reason ?? "the backend refused the update");
      getStatus().then(setSt).catch(() => {});
      return;
    }
    applyStartRef.current = Date.now();
    // The backend is about to restart; poll its status until the version flips (or the
    // pull was a no-op and the sha stays but it comes back up), then prompt a reload for
    // the freshly built UI bundle.
    applyTimerRef.current = setInterval(async () => {
      if (Date.now() - applyStartRef.current > APPLY_TIMEOUT_MS) {
        clearInterval(applyTimerRef.current);
        setPhase("error");
        return;
      }
      try {
        const s = await getStatus();
        setSt(s);
        if (s.current && s.current !== fromShaRef.current) {
          clearInterval(applyTimerRef.current);
          setPhase("done");
        }
      } catch {
        /* backend mid-restart — keep polling */
      }
    }, APPLY_POLL_MS);
  };

  if (!st) return null;

  const behind = st.behind;
  const applying = phase === "applying";
  const btnClass = applying
    ? "border-amber-500/40 bg-amber-500/15 text-amber-300 animate-pulse"
    : phase === "done"
      ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
      : behind > 0
        ? "border-amber-500/40 bg-amber-500/15 text-amber-200 hover:bg-amber-500/25"
        : "border-zinc-700 bg-zinc-800/60 text-zinc-500 hover:text-zinc-300";
  const label = applying
    ? "updating…"
    : phase === "done"
      ? "✓ updated"
      : behind > 0
        ? `⬆ ${behind}`
        : "⬆";

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className={`px-2 py-0.5 rounded border text-[11px] font-medium ${btnClass}`}
        title={
          behind > 0 ? `${behind} update${behind > 1 ? "s" : ""} available` : `up to date · ${st.current ?? "?"}`
        }
      >
        {label}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-50 mt-1 w-80 rounded-lg border border-zinc-700 bg-zinc-900 p-3 shadow-xl text-xs">
            <div className="flex items-center justify-between text-zinc-400">
              <span className="font-semibold uppercase tracking-wide text-zinc-300">Conductor</span>
              <span className="font-mono text-[10px]">
                {st.current ?? "?"}
                {st.branch && st.branch !== "master" ? ` · ${st.branch}` : ""}
              </span>
            </div>

            {refusal && phase === "idle" && (
              <p className="mt-2 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200">
                Update refused: {refusal}
              </p>
            )}

            {phase === "applying" ? (
              <div className="mt-3 flex items-center gap-2 text-amber-300">
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-amber-400 border-t-transparent" />
                <span>Updating &amp; restarting… keep this tab open.</span>
              </div>
            ) : phase === "done" ? (
              <div className="mt-3 space-y-2">
                <p className="text-emerald-300">✓ Updated to {st.current}. Reload to load the new UI.</p>
                <p className="text-[10px] text-zinc-500">Open Claude panes were restarted — reopen them.</p>
                <button
                  onClick={() => window.location.reload()}
                  className="w-full rounded bg-emerald-600 px-3 py-1.5 font-medium text-white hover:bg-emerald-500"
                >
                  Reload now
                </button>
              </div>
            ) : phase === "error" ? (
              <div className="mt-3 space-y-1">
                <p className="text-red-300">Update didn&apos;t confirm in time.</p>
                <p className="text-[10px] text-zinc-500">
                  Check <span className="font-mono">~/.conductor/logs/update.log</span>, or run{" "}
                  <span className="font-mono">bin/conductorctl update</span>.
                </p>
              </div>
            ) : behind > 0 ? (
              <div className="mt-2 space-y-2">
                <div className="max-h-48 overflow-y-auto rounded border border-zinc-800 bg-zinc-950/50">
                  {st.commits.map((c) => (
                    <div key={c.sha} className="border-b border-zinc-800/70 px-2 py-1 last:border-0">
                      <span className="mr-1.5 font-mono text-[10px] text-zinc-500">{c.sha}</span>
                      <span className="text-zinc-200">{c.subject}</span>
                    </div>
                  ))}
                </div>
                {st.updatable ? (
                  <>
                    <button
                      onClick={apply}
                      className="w-full rounded bg-amber-600 px-3 py-1.5 font-medium text-white hover:bg-amber-500"
                    >
                      Update now &amp; restart
                    </button>
                    <p className="text-[10px] text-zinc-500">
                      Fast-forwards this checkout and restarts the backend; open Claude panes will need reopening.
                    </p>
                  </>
                ) : (
                  <p className="text-[11px] text-amber-300/90">
                    Can&apos;t auto-update: {st.blocked_reason}. Update from a terminal with{" "}
                    <span className="font-mono">bin/conductorctl update</span>.
                  </p>
                )}
              </div>
            ) : st.git_error ? (
              // behind === 0 here can just as well mean "git failed and told us nothing" —
              // don't dress that up as a confident "you're on the latest".
              <p className="mt-3 text-[11px] text-amber-300/90">
                Can&apos;t tell: {st.blocked_reason}. Check from a terminal with{" "}
                <span className="font-mono">git status</span>.
              </p>
            ) : (
              <p className="mt-3 text-zinc-400">You&apos;re on the latest ({st.current}).</p>
            )}

            <div className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-2 text-[10px] text-zinc-500">
              <span>
                checked {ago(st.checked_at)}
                {st.fetch_error && <span className="ml-1 text-amber-400/90">⚠ couldn&apos;t reach the remote</span>}
              </span>
              <button
                onClick={check}
                disabled={checking || applying}
                className="rounded px-1.5 py-0.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-50"
              >
                {checking ? "checking…" : "check now"}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
