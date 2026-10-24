import fs from 'fs';
import path from 'path';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
    const env = loadEnv(mode, '.', '');
    const backendPort = env.ARCANEA_API_PORT || '9800';
    const backendTarget = env.ARCANEA_DEV_BACKEND || env.VITE_BACKEND_TARGET || `http://127.0.0.1:${backendPort}`;
    const devPort = parseInt(env.ARCANEA_DEV_PORT || env.VITE_DEV_PORT || '9800', 10);

    const certDir = path.resolve(__dirname, 'certs');
    const certPath = path.join(certDir, 'arcanea-cert.pem');
    const keyPath = path.join(certDir, 'arcanea-key.pem');

    const forceHttps = String(env.ARCANEA_HTTPS || env.VITE_HTTPS || '').trim().toLowerCase();
    const enableHttps = forceHttps === '1' || forceHttps === 'true' || forceHttps === 'yes';

    let https: any = false;
    if (enableHttps) {
      try {
        if (fs.existsSync(certPath) && fs.existsSync(keyPath)) {
          https = {
            cert: fs.readFileSync(certPath),
            key: fs.readFileSync(keyPath),
          };
        }
      } catch (e) {
        https = false;
      }
    }

    return {
      server: {
        // Dev port (keep it >9000 to avoid well-known/common ports).
        port: Number.isFinite(devPort) ? devPort : 9800,
        host: '0.0.0.0',
        https,
        proxy: {
          '/api': {
            target: backendTarget,
            changeOrigin: true,
            secure: false,
            ws: true,
            rewrite: (path: string) => path,
          },
          // Frontend requests absolute `/media-assets/...` URLs for cached posters/backdrops.
          // In dev, the backend serves these at `http://<backend>/media-assets`, so proxy them.
          '/media-assets': {
            target: backendTarget,
            changeOrigin: true,
            secure: false,
          },
        }
      },
      plugins: [react()],
      build: {
        sourcemap: false,
        rollupOptions: {
          output: {
            manualChunks: {
              react: ['react', 'react-dom'],
              vendor: ['lucide-react', 'qrcode'],
            },
          },
        },
      },
      define: {
        'process.env.API_KEY': JSON.stringify(env.GEMINI_API_KEY),
        'process.env.GEMINI_API_KEY': JSON.stringify(env.GEMINI_API_KEY)
      },
      resolve: {
        alias: {
          '@': path.resolve(__dirname, '.'),
        }
      }
    };
});
