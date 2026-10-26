import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.arcanea.media',
  appName: 'ArcaNea',
  webDir: 'dist',
  server: {
    // The WebView origin is plain http on purpose: the client talks to an ArcaNea
    // server on the LAN over HTTP, and an https origin would make those requests
    // mixed content (blocked by the WebView). Same-origin checks still apply.
    androidScheme: 'http',
  },
};

export default config;
