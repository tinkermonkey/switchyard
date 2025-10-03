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
    proxy: {
      '/socket.io': {
        target: 'http://localhost:5001',
        ws: true,
      },
      '/history': 'http://localhost:5001',
      '/claude-logs-history': 'http://localhost:5001',
      '/current-pipeline': 'http://localhost:5001',
    }
  }
})
