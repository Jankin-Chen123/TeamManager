import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig(({ mode }) => ({
  base: loadEnv(mode, ".", "TM_").TM_WEB_BASE || "/",
  plugins: [vue()],
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
}));
