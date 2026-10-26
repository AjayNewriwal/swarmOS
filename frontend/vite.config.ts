import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": api,
      "/health": api,
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});