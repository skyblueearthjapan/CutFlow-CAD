import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// Vite config for CutFlow•CAD frontend.
// - host '0.0.0.0' so docker-compose 上 / VPS から到達できる
// - HMR は WebSocket 5173 で受ける
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
  },
});
