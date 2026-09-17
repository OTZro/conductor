import { useState } from "react";

export function CreateCardModal({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (body: { title: string; summary?: string }) => Promise<void>;
}) {
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!title.trim()) return;
    setBusy(true);
    try {
      await onCreate({ title: title.trim(), summary: summary.trim() || undefined });
      onClose();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[70] bg-black/50 flex items-center justify-center"
      onClick={onClose}
    >
      <div
        className="w-[440px] max-w-[92vw] bg-zinc-900 border border-zinc-700 rounded-xl p-4 space-y-3 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-sm font-semibold text-zinc-100">New card</h2>
        <input
          autoFocus
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            // don't submit on the Enter that confirms an IME composition (中文選字)
            if (e.key === "Enter" && !e.nativeEvent.isComposing) submit();
            if (e.key === "Escape") onClose();
          }}
          placeholder="title"
          className="w-full text-sm px-2.5 py-2 rounded bg-zinc-800 border border-zinc-700 text-zinc-100"
        />
        <textarea
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
          placeholder="notes / links (optional) — PROJ-123 and PR URLs auto-link"
          rows={3}
          className="w-full text-sm px-2.5 py-2 rounded bg-zinc-800 border border-zinc-700 text-zinc-100 resize-none"
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="text-sm px-3 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          >
            Cancel
          </button>
          <button
            disabled={busy || !title.trim()}
            onClick={submit}
            className="text-sm px-3 py-1.5 rounded bg-emerald-500/80 hover:bg-emerald-500 text-zinc-900 font-medium disabled:opacity-40"
          >
            Create
          </button>
        </div>
      </div>
    </div>
  );
}
