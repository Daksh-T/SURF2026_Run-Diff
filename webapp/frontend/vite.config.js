import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    proxy: {
      // the FastAPI backend; the frontend talks to /api/* and it is proxied here in dev
      "/api": "http://127.0.0.1:8077",
    },
  },
});
