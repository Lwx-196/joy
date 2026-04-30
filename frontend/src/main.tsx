import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { BrandProvider } from "./lib/brand-context";
import "./i18n";
import "./index.css";

// Single QueryClient for the whole app.
// staleTime defaults are tuned per-query in src/hooks/queries.ts;
// here we set the global floor + retry policy.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Cases / customers data is "user-edited semi-static": don't refetch on every focus.
      refetchOnWindowFocus: false,
      // Local single-user: 1 retry is enough; 2+ just delays the error toast.
      retry: 1,
      // Default for everything not overridden — short enough that scans show up,
      // long enough that tab switching feels instant.
      staleTime: 30_000,
    },
    mutations: {
      retry: 0,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <BrandProvider>
          <App />
        </BrandProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
