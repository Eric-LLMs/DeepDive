import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy /api to the FastAPI backend during dev so the browser never hits CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://localhost:8300",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      // Serve cached TTS audio / images / avatars straight from the backend.
      "/audio": { target: "http://localhost:8300", changeOrigin: true },
      "/images": { target: "http://localhost:8300", changeOrigin: true },
      "/avatars": { target: "http://localhost:8300", changeOrigin: true },
    },
  },
  // Same proxy for the built bundle served by `vite preview` on a server
  // (scripts/start_server.sh), so the deployed SPA talks to the backend too.
  preview: {
    port: 5273,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://localhost:8300",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/audio": { target: "http://localhost:8300", changeOrigin: true },
      "/images": { target: "http://localhost:8300", changeOrigin: true },
      "/avatars": { target: "http://localhost:8300", changeOrigin: true },
    },
  },
});
