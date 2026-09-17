import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API + WebSocket to the FastAPI backend on :8787.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8787", ws: true },
      // the ttyd proxy — must come through the dev origin too, or the terminal
      // iframe stops being same-origin and TerminalView's reach-into-the-iframe
      // work (reconnect watchdog, selection remap, reveal gating) goes dark.
      "/term": { target: "http://127.0.0.1:8787", changeOrigin: true, ws: true },
    },
  },
});
