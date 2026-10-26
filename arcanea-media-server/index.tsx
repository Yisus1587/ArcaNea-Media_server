import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { I18nProvider } from './i18n/i18n';
import './styles.css';

const shouldClearStorage = (() => {
  try {
    const params = new URLSearchParams(window.location.search || '');
    if (params.get('keep_storage') === '1') return false;
    return true;
  } catch {
    return true;
  }
})();

if (shouldClearStorage) {
  try { localStorage.clear(); } catch {}
  try { sessionStorage.clear(); } catch {}
  try {
    document.cookie.split(';').forEach((cookie) => {
      const name = cookie.split('=')[0]?.trim();
      if (!name) return;
      document.cookie = `${name}=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/`;
    });
  } catch {}
  try {
    if ('caches' in window) {
      caches.keys().then((keys) => keys.forEach((k) => caches.delete(k))).catch(() => {});
    }
  } catch {}
  try {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.getRegistrations().then((regs) => regs.forEach((r) => r.unregister())).catch(() => {});
    }
  } catch {}
}

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error("Could not find root element to mount to");
}

const root = ReactDOM.createRoot(rootElement);

function renderApp() {
  root.render(
    <React.StrictMode>
      <I18nProvider>
        <App />
      </I18nProvider>
    </React.StrictMode>
  );
}

// On the web this resolves to "no setup needed" and renders exactly as before; on a
// native client it restores the server address from Preferences (or asks for it).
async function bootstrap() {
  try {
    const { initServerConfig } = await import('./services/serverConfig');
    const { needsSetup } = await initServerConfig();
    if (needsSetup) {
      const { ServerConnectScreen } = await import('./components/setup/ServerConnectScreen');
      root.render(
        <React.StrictMode>
          <I18nProvider>
            <ServerConnectScreen onConnected={() => renderApp()} />
          </I18nProvider>
        </React.StrictMode>
      );
      return;
    }
  } catch {
    // Never block the web app on native-only bootstrap.
  }
  renderApp();
}

void bootstrap();

if (!shouldClearStorage && import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // ignore
    });
  });
}
