import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built assets are served by FastAPI at /dashboard2 (src/infra_brain/main.py).
// The dev server proxies /api to the local FastAPI app so `npm run dev` works
// against a running backend without CORS config.
export default defineConfig({
  plugins: [react()],
  base: "/dashboard2/",
  build: {
    outDir: "../src/infra_brain/dashboard/static2",
    emptyOutDir: true,
  },
  server: {
    proxy: { "/api": "http://localhost:8000" },
  },
});
