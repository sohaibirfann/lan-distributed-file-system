import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Proxies to the coordinator in dev so relative fetch('/login') paths work without CORS.
// Also used under `preview` so it serves dashboard + API from one origin, like production.
const coordinatorProxy = {
  '/health': 'http://localhost:8000',
  '/register': 'http://localhost:8000',
  '/login': 'http://localhost:8000',
  '/me': 'http://localhost:8000',
  '/nodes': 'http://localhost:8000',
  '/events': 'http://localhost:8000',
  '/files': 'http://localhost:8000',
  '/namespace': 'http://localhost:8000',
  '/config': 'http://localhost:8000',
  '/placement': 'http://localhost:8000',
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: { proxy: coordinatorProxy },
  preview: { proxy: coordinatorProxy },
})
