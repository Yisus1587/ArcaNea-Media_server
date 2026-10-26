import { useState } from 'react';
import { Loader2, QrCode, Server, Wifi } from 'lucide-react';
import { Button } from '../ui/Button';
import { useI18n } from '../../i18n/i18n';
import { adminPinRequired, checkServer, loginWithPin, persistServerUrl } from '../../services/serverConfig';

/**
 * First screen of the native client: where the app finds the ArcaNea server.
 * The address is validated against `/api/health` before being stored.
 */
export function ServerConnectScreen(props: { onConnected: (origin: string) => void }) {
  const { t } = useI18n();
  const [url, setUrl] = useState('');
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState<'check' | 'qr' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reasonFor = (detail: string, status?: number): string => {
    if (detail === 'invalid') return t('server_invalid');
    if (detail === 'http_error') return `${t('server_unreachable')} (HTTP ${status ?? '?'})`;
    if (detail === 'degraded') return t('server_degraded');
    return t('server_unreachable');
  };

  const connect = async () => {
    if (busy) return;
    setBusy('check');
    setError(null);
    try {
      const check = await checkServer(url);
      if (!check.ok) {
        setError(reasonFor(check.detail, check.status));
        return;
      }
      // Every request from a LAN client goes through admin_required, which rejects
      // non-loopback callers unless the server has an admin PIN configured.
      const needsPin = await adminPinRequired(url);
      if (!needsPin) {
        setError(t('server_pin_setup_hint'));
        return;
      }
      if (!pin.trim()) {
        setError(t('server_pin_required'));
        return;
      }
      const authenticated = await loginWithPin(url, pin.trim());
      if (!authenticated) {
        setError(t('server_pin_invalid'));
        return;
      }
      const origin = await persistServerUrl(url);
      if (!origin) {
        setError(t('server_invalid'));
        return;
      }
      props.onConnected(origin);
    } finally {
      setBusy(null);
    }
  };

  const scanQr = async () => {
    if (busy) return;
    setBusy('qr');
    setError(null);
    try {
      const mod: any = await import('@capacitor-mlkit/barcode-scanning');
      const scanner = mod?.BarcodeScanner;
      if (!scanner) {
        setError(t('server_qr_unavailable'));
        return;
      }
      const supported = await scanner.isSupported().catch(() => ({ supported: false }));
      if (!supported?.supported) {
        setError(t('server_qr_unavailable'));
        return;
      }
      const perm = await scanner.requestPermissions();
      if (perm?.camera !== 'granted' && perm?.camera !== 'limited') {
        setError(t('server_qr_permission'));
        return;
      }
      const result = await scanner.scan();
      const value = result?.barcodes?.[0]?.rawValue;
      if (value) setUrl(String(value));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="min-h-screen w-full flex items-center justify-center bg-slate-950 p-6">
      <div className="w-full max-w-md rounded-2xl border border-white/10 bg-slate-900/60 shadow-2xl p-6">
        <div className="flex items-center gap-3">
          <div className="w-11 h-11 rounded-xl bg-indigo-500/15 border border-indigo-400/20 flex items-center justify-center text-indigo-200">
            <Server size={20} />
          </div>
          <div className="min-w-0">
            <div className="text-white font-semibold">{t('server_setup_title')}</div>
            <div className="text-xs text-slate-400">{t('server_setup_hint')}</div>
          </div>
        </div>

        <label className="block mt-6 text-xs font-medium text-slate-300" htmlFor="server-url">
          {t('server_url_label')}
        </label>
        <input
          id="server-url"
          type="url"
          inputMode="url"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void connect();
          }}
          placeholder="http://192.168.1.10:9800"
          className="mt-2 w-full rounded-xl bg-slate-950/70 border border-white/10 px-3 py-2.5 text-sm text-white placeholder:text-slate-500 focus:outline-none focus:border-indigo-400/60 font-mono"
        />

        <label className="block mt-4 text-xs font-medium text-slate-300" htmlFor="server-pin">
          {t('server_pin_label')}
        </label>
        <input
          id="server-pin"
          type="password"
          inputMode="numeric"
          autoComplete="off"
          value={pin}
          onChange={(e) => setPin(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void connect();
          }}
          placeholder="••••"
          className="mt-2 w-full rounded-xl bg-slate-950/70 border border-white/10 px-3 py-2.5 text-sm text-white placeholder:text-slate-500 focus:outline-none focus:border-indigo-400/60"
        />

        {error ? <div className="mt-3 text-xs text-red-300">{error}</div> : null}

        <div className="mt-5 flex flex-col gap-2">
          <Button
            type="button"
            onClick={() => void connect()}
            disabled={!!busy || !url.trim() || !pin.trim()}
            icon={busy === 'check' ? <Loader2 size={16} className="animate-spin" /> : <Wifi size={16} />}
          >
            {busy === 'check' ? t('server_connecting') : t('server_connect')}
          </Button>
          <Button
            type="button"
            variant="ghost"
            onClick={() => void scanQr()}
            disabled={busy === 'check'}
            icon={busy === 'qr' ? <Loader2 size={16} className="animate-spin" /> : <QrCode size={16} />}
          >
            {t('server_scan_qr')}
          </Button>
        </div>
      </div>
    </div>
  );
}
