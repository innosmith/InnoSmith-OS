import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.png'],
      devOptions: { enabled: false },
      manifest: {
        name: 'TaskPilot Cockpit',
        short_name: 'TaskPilot',
        description: 'AI-agentic Task Management Cockpit',
        theme_color: '#4F46E5',
        background_color: '#030712',
        display: 'standalone',
        scope: '/',
        id: '/',
        start_url: '/',
        icons: [
          { src: '/pwa-192.png', sizes: '192x192', type: 'image/png' },
          { src: '/pwa-512.png', sizes: '512x512', type: 'image/png' },
          { src: '/pwa-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        // Neuer SW uebernimmt sofort (kein Warten auf Tab-Schliessung) und beansprucht
        // offene Clients -- zusammen mit registerType 'autoUpdate' erreichen Deploys
        // den Browser ohne manuellen Eingriff (kurzer Auto-Reload).
        skipWaiting: true,
        clientsClaim: true,
        runtimeCaching: [
          {
            // Kein Cache für Streams (SSE) — kann sonst Abort / «network error» verursachen
            urlPattern: ({ url }) =>
              /^\/api\//.test(url.pathname) &&
              !/^\/api\/code\//.test(url.pathname) &&
              !/^\/api\/sse\//.test(url.pathname),
            handler: 'NetworkFirst',
            options: {
              cacheName: 'api-cache',
              expiration: { maxEntries: 50, maxAgeSeconds: 300 },
            },
          },
        ],
      },
    }),
  ],
  build: {
    // mermaid/cytoscape/wardley werden bereits lazy geladen, sind als Drittlibs
    // aber unvermeidbar gross (~440-560 KB). Limit knapp darueber, damit nur
    // echte Ausreisser warnen.
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        // Grosse, eager geladene Vendor-Familien aus dem Entry-Bundle herausloesen.
        // Bewusst NICHT mermaid/cytoscape/katex/wardley anfassen -- die bleiben
        // ueber dynamische Imports lazy.
        manualChunks(id) {
          if (!id.includes('node_modules')) return;
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return 'react-vendor';
          if (id.includes('react-router')) return 'react-router';
          if (id.includes('@tiptap') || id.includes('prosemirror')) return 'editor';
          if (id.includes('recharts')) return 'charts';
          if (id.includes('@dnd-kit')) return 'dnd';
          if (id.includes('lucide-react')) return 'icons';
          if (
            id.includes('react-markdown') ||
            id.includes('/remark') ||
            id.includes('/rehype') ||
            id.includes('/micromark') ||
            id.includes('/unified') ||
            id.includes('/mdast') ||
            id.includes('/hast') ||
            id.includes('/unist')
          ) return 'markdown';
        },
      },
    },
  },
  server: {
    allowedHosts: ['tp.innosmith.ai', 'tp-dev.innosmith.ai', 'tp-int.innosmith.ai'],
    proxy: {
      '/api/code': {
        target: 'http://localhost:8000',
        timeout: 0,
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['cache-control'] = 'no-cache';
            proxyRes.headers['x-accel-buffering'] = 'no';
          });
        },
      },
      '/api/sse': {
        target: 'http://localhost:8000',
        timeout: 0,
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['cache-control'] = 'no-cache';
            proxyRes.headers['x-accel-buffering'] = 'no';
          });
        },
      },
      '/api': 'http://localhost:8000',
      '/uploads': 'http://localhost:8000',
    },
  },
})
