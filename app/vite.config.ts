import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is served same-origin behind Traefik (/api and /ws go to the backend).
// For local `vite dev` we proxy them to the backend on :8000 for convenience.
// @shared-ui is the in-repo shared component package (§shared-ui), living under
// app/ so it is inside the Docker build context and resolves react from
// app/node_modules. The hub vendors this same folder at a pinned ref.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@shared-ui": new URL("./shared-ui/src", import.meta.url).pathname },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
        // VITE_API_HOST lets the target be an IP (macOS resolves *.local via
        // mDNS with a multi-second penalty) while Traefik still routes by Host.
        configure: (proxy) => {
          const host = process.env.VITE_API_HOST;
          if (host) proxy.on("proxyReq", (req) => req.setHeader("host", host));
        },
      },
      "/ws": {
        target: (process.env.VITE_API_TARGET || "http://localhost:8000").replace(/^http/, "ws"),
        ws: true,
        changeOrigin: true,
        configure: (proxy) => {
          const host = process.env.VITE_API_HOST;
          if (host) proxy.on("proxyReq", (req) => req.setHeader("host", host));
        },
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
