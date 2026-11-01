import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import {createHash} from 'node:crypto';

const root = path.dirname(fileURLToPath(import.meta.url));
export default defineConfig(({command}) => ({
  base: '/static/',
  publicDir: false,
  plugins: [react(), {
    name: 'same-origin-entry',
    writeBundle() {
      // Only the three application outputs are replaced. Swagger, vendors and tests stay intact.
      const hash=(name:string)=>createHash('sha256').update(readFileSync(path.join(root,'../frontend',name))).digest('hex').slice(0,12);
      const html = readFileSync(path.join(root, 'index.html'), 'utf8')
        .replace('</head>', '  <link rel="stylesheet" href="/static/styles.css?v='+hash('styles.css')+'" />\n</head>')
        .replace('<script type="module" src="/src/main.tsx"></script>', '<script src="/static/app.js?v='+hash('app.js')+'"></script>');
      writeFileSync(path.join(root, '../frontend/index.html'), html);
    },
  }],
  define: command === 'build' ? {'process.env.NODE_ENV': JSON.stringify('production')} : undefined,
  build: {
    outDir: '../frontend',
    emptyOutDir: false,
    sourcemap: false,
    cssCodeSplit: false,
    lib: {entry: path.join(root, 'src/main.tsx'), name: 'AetherRAG', formats: ['iife'], fileName: () => 'app.js'},
    rollupOptions: {output: {assetFileNames: asset => asset.names?.some(name => name.endsWith('.css')) ? 'styles.css' : '[name][extname]'}},
  },
  server: {proxy: {
    '/api': 'http://127.0.0.1:8000', '/static/vendor': 'http://127.0.0.1:8000',
    '/docs': 'http://127.0.0.1:8000', '/openapi.json': 'http://127.0.0.1:8000',
  }},
}));
