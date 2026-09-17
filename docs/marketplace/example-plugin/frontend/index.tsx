import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import type { PluginMenuProps } from "../../types";

// example-plugin's menu-bar widget — polls the backend's /time endpoint and renders
// it. A MARKETPLACE STARTER KIT (see ../../README.md): copy this file's shape, not
// its content, when authoring a real plugin's frontend package.
//
// "../../types", not "../types": a marketplace install lands this package at
// frontend/src/plugins/local/<name>/index.tsx — one level deeper than a SHIPPED
// plugin's frontend/src/plugins/<name>/index.tsx, where "../types" would be correct.
// Same class of mistake as the backend half of this starter kit ("..base" vs
// "conductor.plugins.base") — install depth, not repo depth, is what the relative
// path has to match.

function ExampleClock() {
  const [time, setTime] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = () =>
      fetch("/api/plugins/example-plugin/time")
        .then((r) => r.json())
        .then((d) => alive && setTime(d.server_time))
        .catch(() => {});
    poll();
    const id = setInterval(poll, 30_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!time) return null;
  return <span className="text-caption text-zinc-400">🕒 {new Date(time).toLocaleTimeString()}</span>;
}

export const MENU_LAYOUTS: Record<string, ComponentType<PluginMenuProps>> = {
  "example-clock": () => <ExampleClock />,
};
