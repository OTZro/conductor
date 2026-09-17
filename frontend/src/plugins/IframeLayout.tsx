import type { PluginTabProps } from "./types";

// The "iframe" tab layout: a reusable primitive that embeds a URL from the plugin's
// manifest config. Self-contained (fills the whole panel) — this is the iframe-plugin
// path, so any embedded tool becomes a tab by declaring layout=iframe + a url.
export function IframeLayout({ config }: PluginTabProps) {
  const url = typeof config?.url === "string" ? config.url : "about:blank";
  return <iframe src={url} title="plugin" className="w-full h-full border-0" />;
}
