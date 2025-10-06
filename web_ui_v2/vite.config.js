import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { TanStackRouterVite } from '@tanstack/router-vite-plugin'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    TanStackRouterVite()
  ],
  server: {
    port: 3000,
    host: '0.0.0.0',
    proxy: {
      '/socket.io': {
        target: 'http://observability-server:5001',
        ws: true,
        changeOrigin: true,
      },
      '/history': {
        target: 'http://observability-server:5001',
        changeOrigin: true,
      },
      '/claude-logs-history': {
        target: 'http://observability-server:5001',
        changeOrigin: true,
      },
      '/current-pipeline': {
        target: 'http://observability-server:5001',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://observability-server:5001',
        changeOrigin: true,
      },
    }
  }
})
