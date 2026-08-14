import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(moduleId) {
          const normalizedModuleId = moduleId.replaceAll("\\\\", "/");
          if (normalizedModuleId.includes("/node_modules/lucide-react/")) {
            return "icons";
          }
          if (normalizedModuleId.includes("/node_modules/@tanstack/react-virtual/")) {
            return "virtual-list";
          }
          if (normalizedModuleId.includes("/node_modules/sql.js/")) {
            return "sql-runtime";
          }
          if (
            normalizedModuleId.includes("/node_modules/react/") ||
            normalizedModuleId.includes("/node_modules/react-dom/") ||
            normalizedModuleId.includes("/node_modules/scheduler/")
          ) {
            return "react-runtime";
          }
          return undefined;
        }
      }
    }
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765"
    }
  }
});
