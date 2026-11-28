import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const backend = "http://127.0.0.1:8080";

const apiRoutes =
  "^/(?:health|chat|events|stream|timeline|conversations|push)(?:[/?]|$)";

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      registerType: "autoUpdate",
      injectRegister: false,
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,ico,png,svg}"],
      },
      manifest: {
        name: "my-agents",
        short_name: "Sora",
        description: "Proactive companion chat",
        start_url: "/",
        scope: "/",
        display: "standalone",
        background_color: "#08090c",
        theme_color: "#08090c",
        icons: [
          {
            src: "/icons/icon-192.png",
            sizes: "192x192",
            type: "image/png",
          },
          {
            src: "/icons/icon-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "any maskable",
          },
        ],
      },
    }),
  ],
  server: {
    proxy: {
      [apiRoutes]: { target: backend, changeOrigin: false },
    },
  },
});
