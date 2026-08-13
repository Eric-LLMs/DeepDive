import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy /api to the FastAPI backend during dev so the browser never hits CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      // Serve cached TTS audio / images straight from the backend.
      "/audio": { target: "http://localhost:8000", changeOrigin: true },
      "/images": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
