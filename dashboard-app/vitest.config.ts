import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Separate from vite.config.ts (not merged) so `npm run build`'s config
// (base: "/dashboard2/", custom outDir) never accidentally affects test
// runs, and vice versa — vitest.config.ts is test-only.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: false,
    // Default 5s times out Security.test.tsx's audit-totals tests under
    // concurrent-agent load on this box (confirmed pre-existing across 5+
    // independent runs, reproduces on an unmodified base commit, not caused
    // by any dashboard-redesign change) - bumped generously rather than
    // per-test to avoid the same flake recurring on other slow tests later.
    testTimeout: 15000,
  },
});
