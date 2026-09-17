import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 3000,
    proxy: {
      "/api/draft": {
        target: "http://drafting:8006",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/draft/, "/draft"),
      },
      "/api": {
        target: "http://alert-service:8004",
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/api/, "") || "/",
      },
      "/ingest": {
        target: "http://video-ingest:8001",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/ingest/, "") || "/",
      },
      "/ws": {
        target: "http://alert-service:8004",
        changeOrigin: true,
        ws: true,
      },
      "/ai": {
        target: "http://traffic-ai:8002",
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/ai/, "") || "/",
      },
    },
  },
});
