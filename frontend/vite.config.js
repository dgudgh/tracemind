import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backendPort = Number.parseInt(process.env.BACKEND_PORT || "8800", 10);
const backendTarget = `http://127.0.0.1:${Number.isFinite(backendPort) ? backendPort : 8800}`;

function attachClientIp(proxy) {
  proxy.on("proxyReq", (proxyReq, req) => {
    proxyReq.setHeader("X-TraceMind-Proxy-Client-IP", req.socket.remoteAddress || "");
  });
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 3300,
    proxy: {
      "/api/v1": {
        target: backendTarget,
        changeOrigin: true,
        configure: attachClientIp,
      },
      "/health": {
        target: backendTarget,
        changeOrigin: true,
        configure: attachClientIp,
      },
    },
  },
  build: {
    outDir: "../build/frontend-dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1250,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("node_modules/react/") || id.includes("node_modules/react-dom/") || id.includes("node_modules/scheduler/")) {
            return "react-vendor";
          }
          if (id.includes("node_modules/antd/") || id.includes("node_modules/rc-") || id.includes("node_modules/@rc-component/") || id.includes("node_modules/@ant-design/")) {
            return "antd-vendor";
          }
          return undefined;
        },
      },
    },
  },
});
