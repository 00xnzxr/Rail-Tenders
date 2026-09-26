import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';
import { readFileSync, writeFileSync, readdirSync, unlinkSync } from 'fs';
import { join } from 'path';

// Chrome content scripts cannot use ES module imports — they run as plain scripts.
// The service worker CAN use imports (manifest has "type": "module").
// The popup CAN use imports (loaded as an HTML page with <script type="module">).
//
// Strategy: Build everything normally with Vite, then post-process content script
// files to replace chunk imports with inlined code using a simple bundle step.

export default defineConfig({
  plugins: [
    react(),
    {
      name: 'bundle-content-scripts',
      closeBundle: async () => {
        const distDir = resolve(__dirname, 'dist');
        const contentScripts = [
          'content-scripts/ireps.js',
          'content-scripts/gem.js',
          'content-scripts/aggregators.js',
        ];

        // Read all chunk files
        const chunksDir = join(distDir, 'chunks');
        const chunkContents: Record<string, string> = {};
        try {
          for (const file of readdirSync(chunksDir)) {
            if (file.endsWith('.js')) {
              chunkContents[`chunks/${file}`] = readFileSync(join(chunksDir, file), 'utf-8');
            }
          }
        } catch {
          // No chunks dir — nothing to inline
          return;
        }

        for (const scriptPath of contentScripts) {
          const fullPath = join(distDir, scriptPath);
          let code: string;
          try {
            code = readFileSync(fullPath, 'utf-8');
          } catch {
            continue;
          }

          // Check if this file has any chunk imports
          if (!code.includes('import')) continue;

          // Collect all imports and the chunks they reference
          const importRegex = /import\s*\{([^}]*)\}\s*from\s*"([^"]+)"\s*;?/g;
          let match;
          const imports: Array<{ full: string; bindings: string; chunkFile: string }> = [];

          while ((match = importRegex.exec(code)) !== null) {
            const relPath = match[2];
            // Normalize: "../chunks/foo.js" → "chunks/foo.js"
            const normalized = relPath.replace(/^\.\.\//, '').replace(/^\.\//, '');
            if (chunkContents[normalized]) {
              imports.push({
                full: match[0],
                bindings: match[1],
                chunkFile: normalized,
              });
            }
          }

          if (imports.length === 0) continue;

          // For each import, we need to:
          // 1. Parse the chunk's export map
          // 2. Wrap chunk code in an IIFE that returns the exports
          // 3. Destructure the IIFE result to get the imported names

          let preamble = '';
          for (const imp of imports) {
            let chunkCode = chunkContents[imp.chunkFile];

            // Parse exports: export{extractDetailPageData as e, extractAllDocumentLinks as a}
            const exportMatch = chunkCode.match(/export\s*\{([^}]*)\}\s*;?/);
            if (!exportMatch) continue;

            // Build export map: alias → localName
            const exportEntries = exportMatch[1].split(',').map(s => s.trim());
            const exportMap: Record<string, string> = {};
            for (const entry of exportEntries) {
              const parts = entry.split(/\s+as\s+/);
              if (parts.length === 2) {
                exportMap[parts[1].trim()] = parts[0].trim();
              } else {
                exportMap[parts[0].trim()] = parts[0].trim();
              }
            }

            // Remove export statement from chunk
            chunkCode = chunkCode.replace(exportMatch[0], '');

            // Also remove any imports within the chunk itself (nested chunks)
            chunkCode = chunkCode.replace(/import\s*\{[^}]*\}\s*from\s*"[^"]*"\s*;?/g, '');

            // Build return object for the IIFE
            const returnEntries = Object.entries(exportMap)
              .map(([alias, local]) => `${alias}: ${local}`)
              .join(', ');

            // Wrap in IIFE
            const iife = `const __chunk_${imp.chunkFile.replace(/[^a-zA-Z0-9]/g, '_')} = (function() {\n${chunkCode}\nreturn {${returnEntries}};\n})();\n`;
            preamble += iife;

            // Parse import bindings: {e as E, a as S}
            const bindingEntries = imp.bindings.split(',').map(s => s.trim());
            for (const binding of bindingEntries) {
              const parts = binding.split(/\s+as\s+/);
              const chunkAlias = parts[0].trim();  // 'e' (the chunk's export alias)
              const localName = parts.length === 2 ? parts[1].trim() : parts[0].trim();
              const iifeVar = `__chunk_${imp.chunkFile.replace(/[^a-zA-Z0-9]/g, '_')}`;
              preamble += `const ${localName} = ${iifeVar}.${chunkAlias};\n`;
            }

            // Remove the import statement from the entry code
            code = code.replace(imp.full, '');
          }

          // Write the inlined file
          writeFileSync(fullPath, preamble + code);
          console.log(`  [bundle-content-scripts] Inlined chunks into ${scriptPath}`);
        }

        // Clean up chunk files only used by content scripts
        // Keep chunks used by popup or service-worker
        const swCode = readFileSync(join(distDir, 'service-worker.js'), 'utf-8');
        let popupCode = '';
        try {
          popupCode = readFileSync(join(distDir, 'popup.js'), 'utf-8');
        } catch { /* no popup.js */ }

        for (const chunkFile of Object.keys(chunkContents)) {
          const chunkName = chunkFile.replace('chunks/', '');
          const usedBySW = swCode.includes(chunkName);
          const usedByPopup = popupCode.includes(chunkName);
          if (!usedBySW && !usedByPopup) {
            try {
              unlinkSync(join(distDir, chunkFile));
              console.log(`  [bundle-content-scripts] Removed unused chunk: ${chunkFile}`);
            } catch { /* ignore */ }
          }
        }
      },
    },
  ],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        popup: resolve(__dirname, 'src/popup/index.html'),
        'service-worker': resolve(__dirname, 'src/background/service-worker.ts'),
        'content-scripts/ireps': resolve(__dirname, 'src/content-scripts/ireps.ts'),
        'content-scripts/gem': resolve(__dirname, 'src/content-scripts/gem.ts'),
        'content-scripts/aggregators': resolve(__dirname, 'src/content-scripts/aggregators.ts'),
      },
      output: {
        entryFileNames: '[name].js',
        chunkFileNames: 'chunks/[name].js',
        assetFileNames: 'assets/[name].[ext]',
      },
    },
  },
  publicDir: 'public',
  test: {
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
  },
});
