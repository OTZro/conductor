import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ClockProvider } from "./clock";
import "./index.css";
// registers the kernel's core default providers (surface.card, surface.terminal,
// registry.layouts) before the first render — see src/kernel/bootstrap.ts
import "./kernel/bootstrap";
import { ToastProvider } from "./ui/toast";

// One shared client. WS events drive invalidation (see queries.ts) so background
// refetch is conservative — no polling storms, refocus refetch stays useful for
// the "phone came back from background" case (§3F.4).
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 10_000, retry: 1, refetchOnWindowFocus: true },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <ClockProvider>
          <App />
        </ClockProvider>
      </ToastProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
