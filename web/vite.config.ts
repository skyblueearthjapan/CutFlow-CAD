import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// Vite config for CutFlow•CAD frontend.
// - host '0.0.0.0' so docker-compose 上 / VPS から到達できる
// - HMR は WebSocket 5173 で受ける
// - /api は同一オリジン (相対パス) で FastAPI に転送 — Tailscale Funnel /
//   nginx 配下でも同じ ``API_BASE='/api'`` で動作する (C2)
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    hmr: {
      host: 'localhost',
      port: 5173,
    },
    watch: {
      // Docker 上で動かす場合に必要 (inotify が効かない環境向け)
      usePolling: true,
      interval: 300,
    },
    proxy: {
      // ``api`` は docker-compose のサービス名。ローカルで vite 単独実行する
      // 場合は ``VITE_API_TARGET=http://localhost:8080`` で上書き可能。
      '/api': {
        target: process.env.VITE_API_TARGET ?? 'http://api:8080',
        changeOrigin: true,
      },
    },
  },
});
