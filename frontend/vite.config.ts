import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (
            id.includes("react-markdown") ||
            id.includes("remark-") ||
            id.includes("micromark") ||
            id.includes("mdast") ||
            id.includes("unified")
          )
            return "markdown";
          if (id.includes("motion")) return "motion";
          if (id.includes("react") || id.includes("scheduler")) return "react-vendor";
          return "vendor";
        }
      }
    }
  },
  test: {
    environment: "jsdom"
  }
});
