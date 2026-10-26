/**
 * Pure helpers for the ArcaNea server address. No React, no Capacitor: this module
 * only turns whatever the user typed (or scanned) into a canonical origin.
 */

/**
 * Normalize a user supplied server address into `protocol//host[:port][/path]`
 * without the trailing `/api`. Accepts bare hosts (`192.168.1.10:9800`) and
 * tolerates a pasted `/api` suffix.
 */
export function normalizeServerOrigin(raw: string | null | undefined): string | null {
  const text = String(raw || '').trim();
  if (!text) return null;
  let url: URL;
  try {
    const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(text) ? text : `http://${text}`;
    url = new URL(withScheme);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  if (!url.hostname) return null;
  let path = url.pathname.replace(/\/+$/, '');
  if (path === '/api') path = '';
  else if (path.endsWith('/api')) path = path.slice(0, -4);
  return `${url.protocol}//${url.host}${path}`;
}

/** API base for an origin: `<origin>/api`, or the relative `/api` when unset. */
export function buildApiBase(origin: string | null): string {
  return origin ? `${origin}/api` : '/api';
}

/** WebSocket origin matching an HTTP origin. */
export function buildWsOrigin(origin: string): string {
  const wsProtocol = origin.startsWith('https:') ? 'wss:' : 'ws:';
  return `${wsProtocol}//${origin.replace(/^https?:\/\//, '')}`;
}

/** Strip the scheme+host from an absolute API base so it can be re-hosted on a ws origin. */
export function apiBasePath(apiBase: string): string {
  return apiBase.replace(/^https?:\/\/[^/]+/, '');
}
