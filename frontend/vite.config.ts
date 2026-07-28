import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Proxies to the coordinator in dev so relative fetch('/login') paths work without CORS.
  server: {
    proxy: {
      '/register': 'http://localhost:8000',
      '/login': 'http://localhost:8000',
      '/me': 'http://localhost:8000',
    },
  },
})
