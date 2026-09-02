import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Mirror the "@/..." alias from tsconfig.json so tests import modules the same way the app does.
    alias: { "@": path.resolve(__dirname, ".") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["**/*.test.tsx", "**/*.test.ts"],
    exclude: ["node_modules/**", ".next/**"],
  },
});
