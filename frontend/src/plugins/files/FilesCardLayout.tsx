import { useState } from "react";
import type { PluginCardProps } from "../types";
import { FilePreview, isPreviewable, type PreviewKind } from "../../components/FilePreview";

// The card-detail "Files" widget: files this card's claude sent via SendUserFile, each a
// download link to the plugin's own router. Read-only; data comes from the plugin's own
// file store (the capture hook stashes them; the provider lists them).
type FileMeta = { name: string; caption: string | null; size: number; sent_at: string };
type FilesData = { card_id: string; files: FileMeta[] };

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function FilesCardLayout({ data }: PluginCardProps) {
  const { card_id, files } = data as FilesData;
  // md/html files open the shared in-app preview on click; ↓ stays a direct download.
  const [preview, setPreview] = useState<{ href: string; filename: string; kind: PreviewKind } | null>(null);
  return (
    <div className="space-y-1">
      {files.map((f) => {
        const href = `/api/plugins/files/${card_id}/${encodeURIComponent(f.name)}`;
        const kind = isPreviewable(f.name, null);
        const rowCls = "flex items-center gap-2 px-2 py-1 rounded border border-zinc-800 bg-zinc-900/40 hover:bg-zinc-800 text-sm";
        if (kind) {
          return (
            <span key={f.name} className={rowCls}>
              <button
                onClick={() => setPreview({ href, filename: f.name, kind })}
                className="flex items-center gap-2 flex-1 min-w-0 text-left"
              >
                <span className="shrink-0">📎</span>
                <span className="font-mono text-zinc-100 truncate">{f.name}</span>
                {f.caption && <span className="text-zinc-400 truncate">— {f.caption}</span>}
              </button>
              <span className="ml-auto shrink-0 text-[11px] text-zinc-500 tabular-nums">{fmtSize(f.size)}</span>
              <a href={href} download={f.name} className="shrink-0 text-sky-400" title="下載">
                ↓
              </a>
            </span>
          );
        }
        return (
          <a key={f.name} href={href} target="_blank" rel="noreferrer" className={rowCls}>
            <span className="shrink-0">📎</span>
            <span className="font-mono text-zinc-100 truncate">{f.name}</span>
            {f.caption && <span className="text-zinc-400 truncate">— {f.caption}</span>}
            <span className="ml-auto shrink-0 text-[11px] text-zinc-500 tabular-nums">{fmtSize(f.size)}</span>
            <span className="shrink-0 text-sky-400">↓</span>
          </a>
        );
      })}
      {preview && (
        <FilePreview
          href={preview.href}
          filename={preview.filename}
          kind={preview.kind}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  );
}
