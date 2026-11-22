import { defineConfig } from 'vite'
import type { Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { viteSingleFile } from 'vite-plugin-singlefile'
import fs from 'fs'
import path from 'path'

const isSingleFile = process.env.BUILD_MODE === 'singlefile'

/**
 * Injects dashboard_data.json as window.__DASHBOARD_DATA__ into the HTML <head>
 * when building in singlefile mode. This makes the report fully self-contained —
 * no fetch() call needed, no server required, works from file:// directly.
 */
function injectDashboardData(): Plugin {
  return {
    name: 'inject-dashboard-data',
    transformIndexHtml(html: string): string {
      const dataPath = path.resolve(__dirname, '../dashboard_data.json')
      if (!fs.existsSync(dataPath)) {
        console.warn('[inject-dashboard-data] dashboard_data.json not found — skipping injection')
        return html
      }
      const data = fs.readFileSync(dataPath, 'utf-8')
      const script = `<script>window.__DASHBOARD_DATA__ = ${data};</script>`
      return html.replace('</head>', `${script}\n  </head>`)
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: isSingleFile
    ? [react(), injectDashboardData(), viteSingleFile()]
    : [react()],
  base: isSingleFile ? './' : '/',
})
