import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  plugins: [
    react(),
    ...(command === "serve"
      ? [
          {
            name: "agentdu-development-csp",
            transformIndexHtml(html: string) {
              return html
                .replace("style-src 'self';", "style-src 'self' 'unsafe-inline';")
                .replace(
                  "connect-src 'self' ws://127.0.0.1:5173;",
                  "connect-src 'self' ws://127.0.0.1:*;",
                );
            },
          },
        ]
      : []),
    ...(command === "build"
      ? [
          {
            name: "agentdu-production-csp",
            transformIndexHtml(html: string) {
              return html.replace("connect-src 'self' ws://127.0.0.1:5173;", "connect-src 'self';");
            },
          },
        ]
      : []),
  ],
  base: "./",
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: "dist/renderer",
    emptyOutDir: true,
  },
}));
