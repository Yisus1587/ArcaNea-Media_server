import { Capacitor } from '@capacitor/core';
import { Preferences } from '@capacitor/preferences';
import { normalizeServerOrigin, setServerOrigin } from './api';

/**
 * Server address handling for the native client build.
 *
 * On the web the app is always served by the same origin as the API, so nothing is
 * stored and `API_BASE` stays relative. On Android/iOS the app is a static bundle
 * inside a WebView, so the address of the LAN server has to be provided once and
 * kept in Capacitor Preferences (index.tsx clears localStorage on every load).
 */

const STORAGE_KEY = 'arcanea_server_url';
const MODE_KEY = 'arcanea_server_mode';
// The API keeps its admin token in localStorage, which index.tsx clears on every
// load; on native the token survives in Preferences and is restored at boot.
const TOKEN_KEY = 'arcanea_admin_token';
const LOCAL_TOKEN_KEY = 'arcanea_admin_token';

export function isNativeApp(): boolean {
  try {
    return Capacitor.isNativePlatform();
  } catch {
    return false;
  }
}

export async function readStoredServerUrl(): Promise<string | null> {
  if (!isNativeApp()) return null;
  try {
    const { value } = await Preferences.get({ key: STORAGE_KEY });
    return normalizeServerOrigin(value);
  } catch {
    return null;
  }
}

/** Validate, apply and (on native) persist a server address. Returns the normalized origin. */
export async function persistServerUrl(raw: string): Promise<string | null> {
  const origin = normalizeServerOrigin(raw);
  if (!origin) return null;
  setServerOrigin(origin);
  if (isNativeApp()) {
    try {
      await Preferences.set({ key: STORAGE_KEY, value: origin });
    } catch {
      // Storage failure must not block a session that already works.
    }
  }
  return origin;
}

/** Does the server require an admin PIN for LAN clients? */
export async function adminPinRequired(raw: string): Promise<boolean> {
  const origin = normalizeServerOrigin(raw);
  if (!origin) return false;
  try {
    const res = await fetch(`${origin}/api/admin/status`, { headers: { Accept: 'application/json' } });
    if (!res.ok) return false;
    const data: any = await res.json().catch(() => null);
    return !!data?.pin_required;
  } catch {
    return false;
  }
}

/**
 * Exchange the admin PIN for a session token and keep it (Preferences on native,
 * localStorage as the runtime copy the API layer reads).
 */
export async function loginWithPin(raw: string, pin: string): Promise<boolean> {
  const origin = normalizeServerOrigin(raw);
  const clean = String(pin || '').trim();
  if (!origin || !clean) return false;
  try {
    const res = await fetch(`${origin}/api/admin/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: clean }),
    });
    if (!res.ok) return false;
    const data: any = await res.json().catch(() => null);
    const token = String(data?.token || '').trim();
    if (!token) return false;
    try {
      localStorage.setItem(LOCAL_TOKEN_KEY, token);
    } catch {
      // ignore
    }
    if (isNativeApp()) {
      try {
        await Preferences.set({ key: TOKEN_KEY, value: token });
      } catch {
        // ignore
      }
    }
    return true;
  } catch {
    return false;
  }
}

async function restoreAdminToken(): Promise<void> {
  if (!isNativeApp()) return;
  try {
    const { value } = await Preferences.get({ key: TOKEN_KEY });
    const token = String(value || '').trim();
    if (token) localStorage.setItem(LOCAL_TOKEN_KEY, token);
  } catch {
    // ignore
  }
}

export async function forgetServerUrl(): Promise<void> {
  setServerOrigin(null);
  if (!isNativeApp()) return;
  try {
    await Preferences.remove({ key: STORAGE_KEY });
  } catch {
    // ignore
  }
}

export type ServerMode = 'local' | 'remote';

export async function readServerMode(): Promise<ServerMode | null> {
  if (!isNativeApp()) return null;
  try {
    const { value } = await Preferences.get({ key: MODE_KEY });
    return value === 'local' || value === 'remote' ? value : null;
  } catch {
    return null;
  }
}

export async function persistServerMode(mode: ServerMode): Promise<void> {
  if (!isNativeApp()) return;
  try {
    await Preferences.set({ key: MODE_KEY, value: mode });
  } catch {
    // ignore
  }
}

export type ServerCheck = {
  ok: boolean;
  detail: 'ok' | 'degraded' | 'invalid' | 'timeout' | 'unreachable' | 'http_error';
  status?: number;
};

/** Probe `<origin>/api/health` (already implemented by the backend). */
export async function checkServer(raw: string, timeoutMs: number = 6000): Promise<ServerCheck> {
  const origin = normalizeServerOrigin(raw);
  if (!origin) return { ok: false, detail: 'invalid' };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    // providers=0: the provider probes do outbound HTTP and can take seconds,
    // which is exactly what this 6s validation timeout must not wait for.
    const res = await fetch(`${origin}/api/health?providers=0`, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    if (!res.ok) return { ok: false, detail: 'http_error', status: res.status };
    const data: any = await res.json().catch(() => null);
    return { ok: true, detail: data && data.ok === false ? 'degraded' : 'ok' };
  } catch (e: any) {
    return { ok: false, detail: e?.name === 'AbortError' ? 'timeout' : 'unreachable' };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Apply the stored server (if any). `needsSetup` is true on a native build with no
 * address yet; on the web it is always false so the app boots exactly as before.
 */
export async function initServerConfig(): Promise<{ needsSetup: boolean; origin: string | null; mode: ServerMode | null }> {
  if (!isNativeApp()) return { needsSetup: false, origin: null, mode: null };

  const mode = await readServerMode();
  const stored = await readStoredServerUrl();

  if (mode === 'local' && stored) {
    // The embedded server dies with the app, so it has to be started again.
    const { startLocalServer } = await import('./localServer');
    const origin = await startLocalServer(Number(new URL(stored).port || 9800));
    if (origin) {
      setServerOrigin(origin);
      return { needsSetup: false, origin, mode };
    }
    return { needsSetup: true, origin: stored, mode };
  }

  if (stored) {
    setServerOrigin(stored);
    await restoreAdminToken();
  }
  return { needsSetup: !stored, origin: stored, mode };
}
