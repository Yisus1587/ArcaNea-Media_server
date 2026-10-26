import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Cloud, CloudOff, Copy, ExternalLink, KeyRound, Loader2, LogOut, RefreshCw, Save } from 'lucide-react';
import { MediaService, getServerOrigin } from '../../services/api';
import { useI18n } from '../../i18n/i18n';
import { Button } from '../ui/Button';

type DriveStatus = {
  authenticated: boolean;
  credentials_present?: boolean;
  client_type?: string | null;
};

const CLOUD_CONSOLE_URL = 'https://console.cloud.google.com/apis/credentials';

/** Open an external URL. Inside the APK the Capacitor browser is used so the
 * login does not happen inside the WebView (which cannot come back from
 * Google). Falls back to window.open on the web. */
async function openExternalUrl(url: string): Promise<boolean> {
  try {
    const caps: any = (globalThis as any).Capacitor;
    if (caps?.isNativePlatform?.()) {
      const { Browser } = await import('@capacitor/browser');
      await Browser.open({ url });
      return true;
    }
  } catch {
    /* plugin unavailable: fall through to the web behaviour */
  }
  try {
    window.open(url, '_blank', 'noopener');
    return true;
  } catch {
    return false;
  }
}

export function GDriveSection() {
  const { t } = useI18n();
  const [status, setStatus] = useState<DriveStatus | null>(null);
  const [jsonText, setJsonText] = useState('');
  const [busy, setBusy] = useState<'save' | 'connect' | 'disconnect' | null>(null);
  const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);
  const [redirectUri, setRedirectUri] = useState<string | null>(null);
  const lastAuthUrlRef = useRef<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await MediaService.getGDriveStatus();
      setStatus(s);
    } catch {
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Coming back from the Google login happens in another app (or another
  // browser tab), so the WebView is foregrounded again: that is the moment to
  // re-check whether the token arrived. Web has no equivalent event, hence the
  // "Ya conecté" button too.
  useEffect(() => {
    let listener: any = null;
    let disposed = false;
    (async () => {
      try {
        const caps: any = (globalThis as any).Capacitor;
        if (!caps?.isNativePlatform?.()) return;
        const { App } = await import('@capacitor/app');
        const sub = await App.addListener('appStateChange', ({ isActive }: { isActive: boolean }) => {
          if (isActive) void refresh();
        });
        if (disposed) {
          await sub?.remove?.();
        } else {
          listener = sub;
        }
      } catch {
        /* no-op */
      }
    })();
    return () => {
      disposed = true;
      try {
        listener?.remove?.();
      } catch {
        /* no-op */
      }
    };
  }, [refresh]);

  const serverIsLocal = useMemo(() => {
    const origin = getServerOrigin();
    if (!origin) return true; // same-origin web: the server is this machine
    try {
      const host = new URL(origin).hostname;
      return host === 'localhost' || host === '127.0.0.1' || host === '[::1]';
    } catch {
      return false;
    }
  }, [status]);

  const handleSaveJson = async () => {
    setMessage(null);
    let parsed: any;
    try {
      parsed = JSON.parse(jsonText);
    } catch {
      setMessage({ kind: 'error', text: t('gdrive_json_invalid') });
      return;
    }
    if (!parsed || typeof parsed !== 'object' || (!parsed.installed && !parsed.web)) {
      setMessage({ kind: 'error', text: t('gdrive_json_invalid') });
      return;
    }
    setBusy('save');
    try {
      await MediaService.uploadGDriveCredentials(parsed);
      setJsonText('');
      setMessage({ kind: 'ok', text: t('gdrive_json_saved') });
      await refresh();
    } catch (e: any) {
      setMessage({ kind: 'error', text: e?.message || t('gdrive_json_invalid') });
    } finally {
      setBusy(null);
    }
  };

  const handleFile = (file: File | undefined | null) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      setJsonText(String(reader.result || ''));
      setMessage(null);
    };
    reader.readAsText(file);
  };

  const handleConnect = async () => {
    setMessage(null);
    setBusy('connect');
    try {
      const res = await MediaService.getGDriveAuthUrl();
      lastAuthUrlRef.current = res.auth_url || null;
      setRedirectUri(res.redirect_uri || null);
      const opened = await openExternalUrl(res.auth_url);
      if (!opened) {
        setMessage({ kind: 'error', text: t('gdrive_error') });
      }
    } catch (e: any) {
      setMessage({ kind: 'error', text: e?.message || t('gdrive_error') });
    } finally {
      setBusy(null);
    }
  };

  const handleCopyLink = async () => {
    const url = lastAuthUrlRef.current;
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setMessage({ kind: 'ok', text: t('gdrive_link_copied') });
    } catch {
      setMessage({ kind: 'error', text: url });
    }
  };

  const handleDisconnect = async () => {
    setBusy('disconnect');
    try {
      await MediaService.disconnectGDrive();
      await refresh();
    } catch {
      setMessage({ kind: 'error', text: t('gdrive_error') });
    } finally {
      setBusy(null);
    }
  };

  const statusBadge = !status?.credentials_present
    ? { label: t('gdrive_status_none'), cls: 'bg-slate-500/20 text-slate-300', Icon: KeyRound }
    : status.authenticated
      ? { label: t('gdrive_status_ok'), cls: 'bg-emerald-500/20 text-emerald-300', Icon: Cloud }
      : { label: t('gdrive_status_anon'), cls: 'bg-amber-500/20 text-amber-300', Icon: CloudOff };

  return (
    <div id="gdrive-section" className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0b1220] via-[#0f172a] to-[#0b1220] p-6 shadow-[0_20px_60px_-45px_rgba(15,23,42,0.9)]">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-xl font-semibold flex items-center text-white"><Cloud className="mr-3 text-cyan-400" /> {t('gdrive_title')}</h2>
        <span className={`text-xs px-3 py-1 rounded-full flex items-center gap-1 ${statusBadge.cls}`}>
          <statusBadge.Icon size={12} /> {statusBadge.label}
        </span>
      </div>
      <p className="text-sm text-slate-400 mb-5">{t('gdrive_once_hint')}</p>

      {message && (
        <div className={`mb-4 text-sm rounded-xl px-4 py-3 border ${message.kind === 'ok' ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-200' : 'bg-red-500/10 border-red-500/30 text-red-200'}`}>
          {message.text}
        </div>
      )}

      <details className="mb-5 bg-white/5 rounded-2xl border border-white/10 p-4">
        <summary className="cursor-pointer text-sm text-slate-200 font-medium">{t('gdrive_guide_title')}</summary>
        <ol className="mt-3 space-y-1.5 text-sm text-slate-400 list-decimal list-inside">
          <li>{t('gdrive_guide_1')}</li>
          <li>{t('gdrive_guide_2')}</li>
          <li>{t('gdrive_guide_3')}</li>
          <li>{t('gdrive_guide_4')}</li>
          <li>{t('gdrive_guide_5')}</li>
          <li>{t('gdrive_guide_6')}</li>
        </ol>
        <a href={CLOUD_CONSOLE_URL} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 mt-3 text-xs text-cyan-400 hover:text-cyan-300">
          {t('gdrive_guide_console')} <ExternalLink size={12} />
        </a>
      </details>

      {!status?.credentials_present && (
        <div className="space-y-3">
          <textarea
            value={jsonText}
            onChange={e => setJsonText(e.target.value)}
            placeholder={t('gdrive_paste_json')}
            rows={5}
            spellCheck={false}
            className="w-full bg-black/40 border border-white/10 rounded-xl px-4 py-2.5 text-xs font-mono text-white focus:ring-2 focus:ring-cyan-500 outline-none"
          />
          <div className="flex flex-wrap gap-3">
            <input
              ref={fileInputRef}
              type="file"
              accept="application/json,.json"
              className="hidden"
              onChange={e => handleFile(e.target.files?.[0])}
            />
            <Button type="button" variant="secondary" onClick={() => fileInputRef.current?.click()}>{t('gdrive_load_file')}</Button>
            <Button type="button" onClick={handleSaveJson} disabled={!jsonText.trim() || busy === 'save'} icon={busy === 'save' ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}>
              {t('gdrive_save_json')}
            </Button>
          </div>
        </div>
      )}

      {status?.credentials_present && (
        <div className="space-y-3">
          {redirectUri && status.client_type === 'web' && (
            <p className="text-xs text-slate-400 break-all">
              {t('gdrive_redirect_note')} <code className="text-amber-300">{redirectUri}</code>
            </p>
          )}
          {!serverIsLocal && (
            <p className="text-xs text-amber-300/90 bg-amber-500/10 border border-amber-500/30 rounded-xl px-4 py-3">
              {t('gdrive_remote_hint')}
            </p>
          )}
          <div className="flex flex-wrap gap-3">
            {!status.authenticated && (
              <>
                <Button type="button" onClick={handleConnect} disabled={busy === 'connect'} icon={busy === 'connect' ? <Loader2 size={16} className="animate-spin" /> : <ExternalLink size={16} />}>
                  {t('gdrive_connect')}
                </Button>
                {lastAuthUrlRef.current && (
                  <Button type="button" variant="ghost" onClick={handleCopyLink} icon={<Copy size={16} />}>{t('gdrive_copy_link')}</Button>
                )}
              </>
            )}
            {status.authenticated && (
              <Button type="button" variant="ghost" onClick={handleDisconnect} disabled={busy === 'disconnect'} icon={<LogOut size={16} />}>
                {t('gdrive_disconnect')}
              </Button>
            )}
            <Button type="button" variant="ghost" onClick={() => void refresh()} icon={<RefreshCw size={16} />}>
              {t('gdrive_already')}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}


