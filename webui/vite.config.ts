import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build output goes into the Python package so the gateway serves it directly.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../ragnarbot/web/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:18792',
      '/ws': { target: 'ws://127.0.0.1:18792', ws: true },
    },
  },
})
