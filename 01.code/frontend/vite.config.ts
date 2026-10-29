import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

/**
 * 开发服务器把 `/api` 代理到后端（默认 127.0.0.1:8000），
 * 好处：前端同源调用，**不依赖后端 CORS 配置**，SSE 也不受跨域限制。
 * 后端端口可用环境变量 `VITE_BACKEND` 覆盖。
 */
const backend = process.env.VITE_BACKEND ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  server: {
    port: 5173,
    strictPort: false,
    // 绑 IPv4 全部网卡（`0.0.0.0`）：
    // ① 仍包含 127.0.0.1，无头截图/回归脚本照旧可用；
    // ② 局域网设备（手机等）可用 `http://<本机内网IP>:5173` 访问，见实训附录B 步骤7；
    //    `/api` 由本机代理转发到 127.0.0.1:8000，所以**不需要**把 8000 也暴露出去；
    // ③ 不用默认的 `::1`（只监听 IPv6 回环）——那会让 127.0.0.1 都连不上。
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
        // SSE 需要关闭代理缓冲（后端已带 Cache-Control: no-cache）
        ws: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        manualChunks: {
          plotly: ['plotly.js-dist-min'],
          antd: ['antd', '@ant-design/icons'],
        },
      },
    },
  },
});
