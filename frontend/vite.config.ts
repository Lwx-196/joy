import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:5191";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5175,
    host: "127.0.0.1",
    proxy: {
      "/api": apiTarget,
      "/healthz": apiTarget,
    },
  },
  build: {
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        // Match by absolute module id under node_modules so that React's
        // internal modules (jsx-runtime / scheduler) all land in vendor-react,
        // and so that future deps (e.g. react-virtuoso) don't accidentally
        // join a vendor bundle they shouldn't.
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (/[\\/]node_modules[\\/]react-router/.test(id)) return "vendor-router";
          if (/[\\/]node_modules[\\/]@tanstack[\\/]react-query/.test(id)) return "vendor-query";
          if (/[\\/]node_modules[\\/](?:i18next|react-i18next|i18next-browser-languagedetector)[\\/]/.test(id)) return "vendor-i18n";
          if (/[\\/]node_modules[\\/](?:axios|zustand)[\\/]/.test(id)) return "vendor-misc";
          if (/[\\/]node_modules[\\/](?:react|react-dom|scheduler)[\\/]/.test(id)) return "vendor-react";
        },
      },
    },
  },
});
