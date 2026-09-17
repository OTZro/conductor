import { useState } from "react";
import type { PluginCardProps } from "../types";

// The fork row in the terminal's ⋯ menu (card_widget slot="menu"): drives claude's
// own /fork in the live pane — the current window keeps the ORIGINAL conversation,
// and a second conductor terminal opens on the forked copy (it appears in the card's
// session list via the touch_card refresh). Backend: POST /api/plugins/fork/{card}.
type ForkData = { card_id: string };

export function ForkMenuLayout({ data }: PluginCardProps) {
  const { card_id } = data as ForkData;
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [msg, setMsg] = useState<string | null>(null);

  const fork = async () => {
    setState("busy");
    setMsg(null);
    try {
      const r = await fetch(`/api/plugins/fork/${card_id}`, { method: "POST" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body?.detail || `HTTP ${r.status}`);
      setState("done");
      setMsg(`forked ${body.fork} ✓`);
    } catch (e: any) {
      setState("error");
      setMsg(String(e?.message || e));
    }
  };

  return (
    <button
      disabled={state === "busy"}
      onClick={fork}
      className="px-2 py-1 rounded text-left text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
      title="claude /fork — copy this conversation into a second window; this one keeps working"
    >
      {state === "busy" ? (
        <span className="text-amber-400 animate-pulse">⑂ forking…</span>
      ) : state === "done" ? (
        <span className="text-emerald-400">⑂ {msg}</span>
      ) : state === "error" ? (
        <span className="text-red-400" title={msg || undefined}>⑂ fork failed — retry</span>
      ) : (
        <>⑂ fork session</>
      )}
    </button>
  );
}
