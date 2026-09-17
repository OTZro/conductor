import { useCallback, useEffect, useRef, useState } from "react";
import { logout } from "../api";
import type { AuthUser } from "../api";
import { ThemePickerPanel, useThemeEngine } from "./ThemePicker";

/** Header's single right-most control: avatar button + dropdown. Consolidates what used
 * to be four separate controls (theme picker, email pill, sign-out menu, status dot) —
 * the dropdown carries email, connection health, theme, and sign-out, in that order.
 *
 * Rendered even when signed out / auth is off (as a placeholder gear avatar, no
 * email/sign-out rows) so the health section and theme picker — a display preference —
 * stay reachable without a session. `useThemeEngine` runs unconditionally here rather
 * than only while the dropdown is open, since it applies the palette to the whole page. */
export function AvatarMenu({
  user,
  onSignedOut,
  connected,
  stale,
}: {
  user: AuthUser | null;
  onSignedOut: () => void;
  connected: boolean;
  stale: string[]; // delayed/failing data sources
}) {
  const [open, setOpen] = useState(false);
  const [themeOpen, setThemeOpen] = useState(false); // accordion inside the dropdown, collapsed by default
  const rootRef = useRef<HTMLDivElement>(null);
  const themeEngine = useThemeEngine();

  useEffect(() => {
    if (!open) return;
    setThemeOpen(false); // re-collapse every time the dropdown opens
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  const doSignOut = useCallback(async () => {
    try {
      await logout();
    } catch {
      /* ignore */
    }
    setOpen(false);
    onSignedOut();
  }, [onSignedOut]);

  const ringColor = !connected ? "ring-zinc-600" : stale.length ? "ring-amber-400" : "ring-emerald-400";
  const healthLabel = !connected
    ? "offline"
    : stale.length
      ? `data sources delayed/failing:\n${stale.join("\n")}`
      : "live · all sources healthy";
  const dotColor = !connected ? "bg-zinc-600" : stale.length ? "bg-amber-400" : "bg-emerald-400";

  return (
    <div className="relative shrink-0" ref={rootRef}>
      <button
        onClick={() => setOpen((o) => !o)}
        data-testid="user-chip"
        title={user ? user.email : healthLabel}
        aria-label={user ? `帳號選單,${user.email}` : "設定選單"}
        className={`shrink-0 w-7 h-7 rounded-full ring-2 ${ringColor} overflow-hidden flex items-center justify-center bg-zinc-800 hover:bg-zinc-700`}
      >
        {user?.picture ? (
          <img src={user.picture} alt="" className="w-full h-full object-cover" />
        ) : user ? (
          <span className="w-full h-full flex items-center justify-center bg-sky-600 text-white text-xs font-bold">
            {(user.email[0] || "?").toUpperCase()}
          </span>
        ) : (
          <span className="text-xs text-zinc-400">⚙</span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 w-72 max-h-[80vh] overflow-y-auto rounded-modal border border-zinc-700 bg-surface-raised shadow-xl p-3 space-y-3 text-body-s">
          {user && <div className="px-1 py-0.5 text-zinc-400 truncate">{user.email}</div>}

          <div className="px-2 py-1.5 rounded-chip bg-surface-hover border border-zinc-700 text-zinc-300 whitespace-pre-line">
            <span className={`inline-block w-2 h-2 rounded-full mr-1.5 align-middle ${dotColor}`} />
            {healthLabel}
          </div>

          <div className="border-t border-zinc-800 pt-2">
            <button
              onClick={() => setThemeOpen((o) => !o)}
              className="w-full flex items-center justify-between px-2 py-1.5 rounded-chip bg-surface-hover border border-zinc-700 text-zinc-300 hover:text-zinc-100"
            >
              <span>Theme · {themeEngine.current.label}</span>
              <span className={`text-zinc-500 transition-transform ${themeOpen ? "rotate-180" : ""}`}>▾</span>
            </button>
            {themeOpen && (
              <div className="pt-2">
                <ThemePickerPanel engine={themeEngine} />
              </div>
            )}
          </div>

          {user && (
            <button
              onClick={doSignOut}
              data-testid="sign-out"
              className="w-full text-left px-2 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 text-red-300"
            >
              Sign out
            </button>
          )}
        </div>
      )}
    </div>
  );
}
