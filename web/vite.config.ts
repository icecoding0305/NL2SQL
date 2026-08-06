import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发环境把 /api 代理到后端(含 WebSocket),前端无需关心跨域
// 后端端口可用环境变量覆盖:VITE_PROXY_TARGET=http://localhost:8001
export default defineConfig({
  plugins: [react()],
  server: {
    // 监听所有接口,避免只监听 IPv6 [::1] 导致浏览器(IPv4)访问 localhost 打不开
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_PROXY_TARGET || "http://localhost:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
