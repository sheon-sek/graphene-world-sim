/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // Relative asset paths, so a build can be served from any path (and as a review artifact).
  base: "./",
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
  },
});
