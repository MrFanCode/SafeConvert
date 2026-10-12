import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /api to the local FastAPI backend so the frontend
// runs same-origin in development exactly as it does behind nginx in
// production. That matters for testing: no CORS, and the fetch paths that
// ship are the paths that get tested.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    // Fail loudly if the bundle grows unexpectedly. The whole app is two
    // runtime dependencies, so anything approaching this means something new
    // got added and should be reviewed.
    chunkSizeWarningLimit: 300,
  },
})