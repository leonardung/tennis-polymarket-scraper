import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build lands inside the Python package, because that is what
// `polymarket dashboard` serves -- FastAPI mounts this directory at /static and
// returns its index.html at /. The built assets are committed for the same
// reason: installing the package must not require a Node toolchain.
export default defineConfig({
  plugins: [react()],
  base: "/static/",
  build: {
    outDir: "../src/polymarket/dashboard/static",
    emptyOutDir: true,
    sourcemap: false,
    // One bundle rather than many small chunks: it is served from localhost, so
    // request count matters more than cacheability.
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  server: {
    port: 5173,
    // `npm run dev` gives hot reload against a dashboard already serving the
    // real database: run `polymarket dashboard --no-open` alongside it.
    proxy: {
      "/api": "http://127.0.0.1:8787",
    },
  },
});
