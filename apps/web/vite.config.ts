/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => ({
  // Relative asset paths, so a build can be served from any path (and as a review artifact).
  base: "./",
  plugins: [react()],
  server: {
    // `pnpm web:dev` against `gws_api.serve` on its default port.
    proxy: { "/api": { target: loadEnv(mode, ".", "GWS_").GWS_API ?? "http://127.0.0.1:8000", ws: true } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
}));
