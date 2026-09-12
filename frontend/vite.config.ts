import { svelte } from '@sveltejs/vite-plugin-svelte'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  server: {
    // Proxying /api during development keeps the backend free of CORS handling:
    // in production both are served from the same origin anyway.
    proxy: { '/api': 'http://127.0.0.1:8430' },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
