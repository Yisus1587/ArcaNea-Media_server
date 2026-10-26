import { useEffect, useState } from 'react';
import { FolderLock, HardDrive, Loader2, QrCode, Server, Smartphone, Wifi } from 'lucide-react';
import { Button } from '../ui/Button';
import { useI18n } from '../../i18n/i18n';
import {
  LOCAL_SERVER_DEFAULT_PORT,
  localServerSupported,
  readLocalServerStatus,
  requestAllFilesAccess,
  startLocalServer,
} from '../../services/localServer';
import { adminPinRequired, checkServer, loginWithPin, persistServerMode, persistServerUrl } from '../../services/serverConfig';

type Mode = 'local' | 'remote';

/**
 * First screen of the native app: run the server on the phone itself, or point the
 * app at an ArcaNea server already running on the local network.
 */
export function ServerConnectScreen(props: { onConnected: (origin: string) => void }) {
  const { t } = useI18n();
  const [mode, setMode] = useState<Mode>('local');
  const [port, setPort] = useState(String(LOCAL_SERVER_DEFAULT_PORT));
  const [url, setUrl] = useState('');
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState<'local' | 'remote' | 'qr' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [allFiles, setAllFiles] = useState(false);

  useEffect(() => {
    if (!localServerSupported()) {
      setMode('remote');
      return;
    }
    void readLocalServerStatus().then((status) => {
      if (status) setAllFiles(status.allFiles);
    });
  }, []);

  const reasonFor = (detail: string, status?: number): string => {
    if (detail === 'invalid') return t('server_invalid');
    if (detail === 'http_error') return `${t('server_unreachable')} (HTTP ${status ?? '?'})`;
    if (detail === 'degraded') return t('server_degraded');
    return t('server_unreachable');
  };

  const connectLocal = async () => {
    if (busy) return;
    setBusy('local');
    setError(null);
    try {
      const parsed = Number(port);
      const safePort = Number.isFinite(parsed) && parsed > 1023 && parsed < 65536 ? Math.floor(parsed) : LOCAL_SERVER_DEFAULT_PORT;
      const origin = await startLocalServer(safePort);
      if (!origin) {
        setError(t('server_local_failed'));
        return;
      }
      await persistServerUrl(origin);
      await persistServerMode('local');
      props.onConnected(origin);
    } finally {
      setBusy(null);
    }
  };

  const connectRemote = async () => {
    if (busy) return;
    setBusy('remote');
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
      await persistServerMode('remote');
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

  const askAllFiles = async () => {
    const granted = await requestAllFilesAccess();
    setAllFiles(granted);
    if (!granted) {
      setError(t('server_all_files_opened'));
    }
  };

  const inputClass =
    'mt-2 w-full rounded-xl bg-slate-950/70 border border-white/10 px-3 py-2.5 text-sm text-white placeholder:text-slate-500 focus:outline-none focus:border-indigo-400/60';

  return (
    <div className="min-h-dvh w-full flex items-center justify-center bg-slate-950 p-6 pt-[calc(1.5rem+env(safe-area-inset-top,0px))] pb-[calc(1.5rem+env(safe-area-inset-bottom,0px))] overflow-y-auto">
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

        <div className="mt-5 flex items-center rounded-lg border border-white/10 overflow-hidden">
          <button
            type="button"
            disabled={!localServerSupported()}
            onClick={() => {
              setMode('local');
              setError(null);
            }}
            className={`flex-1 px-3 py-2 text-xs flex items-center justify-center gap-1.5 transition ${
              mode === 'local' ? 'bg-white/10 text-white' : 'text-slate-400 hover:text-white'
            } ${localServerSupported() ? '' : 'opacity-40 cursor-not-allowed'}`}
          >
            <Smartphone size={13} />
            {t('server_mode_local')}
          </button>
          <button
            type="button"
            onClick={() => {
              setMode('remote');
              setError(null);
            }}
            className={`flex-1 px-3 py-2 text-xs flex items-center justify-center gap-1.5 border-l border-white/10 transition ${
              mode === 'remote' ? 'bg-white/10 text-white' : 'text-slate-400 hover:text-white'
            }`}
          >
            <Wifi size={13} />
            {t('server_mode_remote')}
          </button>
        </div>

        {mode === 'local' ? (
          <>
            <label className="block mt-5 text-xs font-medium text-slate-300" htmlFor="server-port">
              {t('server_port_label')}
            </label>
            <input
              id="server-port"
              type="number"
              inputMode="numeric"
              value={port}
              onChange={(e) => setPort(e.target.value)}
              placeholder={String(LOCAL_SERVER_DEFAULT_PORT)}
              className={`${inputClass} font-mono`}
            />

            <div className="mt-4 rounded-xl border border-white/10 bg-slate-950/50 p-3">
              <div className="flex items-center gap-2 text-xs text-slate-300">
                <FolderLock size={14} />
                <span className="font-medium">{t('server_all_files')}</span>
                <span className={`ml-auto ${allFiles ? 'text-emerald-300' : 'text-amber-300'}`}>
                  {allFiles ? t('server_all_files_granted') : t('server_all_files_missing')}
                </span>
              </div>
              <div className="mt-1 text-[11px] text-slate-500">{t('server_all_files_hint')}</div>
              <Button type="button" size="sm" variant="ghost" className="mt-2" onClick={() => void askAllFiles()}>
                {t('server_all_files_open')}
              </Button>
            </div>
          </>
        ) : (
          <>
            <label className="block mt-5 text-xs font-medium text-slate-300" htmlFor="server-url">
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
              placeholder="http://192.168.1.10:9800"
              className={`${inputClass} font-mono`}
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
              placeholder="••••"
              className={inputClass}
            />
          </>
        )}

        {error ? <div className="mt-3 text-xs text-red-300">{error}</div> : null}

        <div className="mt-5 flex flex-col gap-2">
          {mode === 'local' ? (
            <Button
              type="button"
              onClick={() => void connectLocal()}
              disabled={!!busy}
              icon={busy === 'local' ? <Loader2 size={16} className="animate-spin" /> : <Smartphone size={16} />}
            >
              {busy === 'local' ? t('server_starting') : t('server_start_local')}
            </Button>
          ) : (
            <>
              <Button
                type="button"
                onClick={() => void connectRemote()}
                disabled={!!busy || !url.trim() || !pin.trim()}
                icon={busy === 'remote' ? <Loader2 size={16} className="animate-spin" /> : <HardDrive size={16} />}
              >
                {busy === 'remote' ? t('server_connecting') : t('server_connect')}
              </Button>
              <Button
                type="button"
                variant="ghost"
                onClick={() => void scanQr()}
                disabled={busy === 'remote'}
                icon={busy === 'qr' ? <Loader2 size={16} className="animate-spin" /> : <QrCode size={16} />}
              >
                {t('server_scan_qr')}
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
