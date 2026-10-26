import { MediaItem, ScanStatus, Drive } from '../types';
import { MOCK_MEDIA } from '../constants';
import { getUiLang } from '../i18n/i18n';
import { apiBasePath, buildApiBase, buildWsOrigin, normalizeServerOrigin } from './serverUrl';

export { normalizeServerOrigin };

// Relative on the web build (Vite and FastAPI serve the same origin), or an absolute
// base such as `http://192.168.1.10:9800/api` when the app runs as a native client
// against a server on the LAN.
let API_BASE = '/api';
let serverOrigin: string | null = null;

/** Point every request (HTTP, WebSocket, SSE) at a server; `null` restores the web behavior. */
export function setServerOrigin(raw: string | null | undefined): void {
  serverOrigin = normalizeServerOrigin(raw);
  API_BASE = buildApiBase(serverOrigin);
}

export function getServerOrigin(): string | null {
  return serverOrigin;
}

let adminMode = false;
let telemetryToken = '';

export function setAdminMode(flag: boolean) {
  adminMode = !!flag;
}

function setTelemetryToken(token: string | null | undefined) {
  const t = String(token || '').trim();
  if (t) telemetryToken = t;
}

function getAdminToken(): string {
  try {
    return (localStorage.getItem('arcanea_admin_token') || '').trim();
  } catch {
    return '';
  }
}

function adminHeaders(): HeadersInit | undefined {
  if (!adminMode) return undefined;
  const headers: Record<string, string> = { 'X-Arcanea-Manager': '1', 'X-Arcanea-Role': 'admin' };
  try {
    const token = getAdminToken();
    if (token) headers['X-Arcanea-Admin-Token'] = token;
  } catch {
    // ignore
  }
  return headers;
}

function adminHeadersAny(): HeadersInit | undefined {
  const token = getAdminToken();
  if (!token) return adminHeaders();
  return {
    'X-Arcanea-Manager': '1',
    'X-Arcanea-Role': 'admin',
    'X-Arcanea-Admin-Token': token,
  };
}

function profileHeaders(profileId?: string): HeadersInit | undefined {
  const pid = (profileId || '').trim();
  if (!pid) return undefined;
  return { 'X-Arcanea-Profile-Id': pid };
}

function getBrowserOrigins(): { origin: string; wsOrigin: string } | null {
  try {
    if (serverOrigin) {
      return { origin: serverOrigin, wsOrigin: buildWsOrigin(serverOrigin) };
    }
    if (typeof window === 'undefined') return null;
    const protocol = window.location.protocol || 'http:';
    const host = window.location.host;
    if (!host) return null;
    const origin = `${protocol}//${host}`;
    const wsProtocol = protocol === 'https:' ? 'wss:' : 'ws:';
    const wsOrigin = `${wsProtocol}//${host}`;
    return { origin, wsOrigin };
  } catch {
    return null;
  }
}

function _getDirectApiBases(): string[] {
  if (API_BASE.startsWith('http')) return [API_BASE];
  // Always mirror the browser origin (protocol/host/port).
  const origins = getBrowserOrigins();
  if (!origins) return [];
  return [`${origins.origin}${API_BASE}`];
}

/** Build a WebSocket/SSE friendly base for the given origin, honouring an absolute API base. */
function _apiBaseFor(origin: string): string {
  if (API_BASE.startsWith('http')) {
    return `${origin}${apiBasePath(API_BASE)}`;
  }
  return `${origin}${API_BASE}`;
}

export function getStatusWsUrls(): string[] {
  try {
    if (typeof window === 'undefined') return [];
    const token = getAdminToken();
    if (!token) return [];
    const origins = getBrowserOrigins();
    if (!origins) return [];
    const sameHost = `${_apiBaseFor(origins.wsOrigin)}/ws/status?admin_token=${encodeURIComponent(token)}`;
    return [sameHost];
  } catch {
    return [];
  }
}

export function getTelemetrySseUrls(): string[] {
  try {
    if (typeof window === 'undefined') return [];
    const origins = getBrowserOrigins();
    if (!origins) return [];
    const token = getAdminToken();
    const urls: string[] = [];
    if (telemetryToken) {
      urls.push(`${_apiBaseFor(origins.origin)}/telemetry/stream?telemetry_token=${encodeURIComponent(telemetryToken)}`);
    }
    if (token) {
      urls.push(`${_apiBaseFor(origins.origin)}/telemetry/stream?admin_token=${encodeURIComponent(token)}`);
    }
    // Only include unauthenticated fallback when no token is available, to avoid 403 retry loops.
    if (!urls.length) {
      urls.push(`${_apiBaseFor(origins.origin)}/telemetry/stream`);
    }
    return urls;
  } catch {
    return [];
  }
}

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

function _toNumber(v: any): number | undefined {
  if (v == null) return undefined;
  const n = typeof v === 'number' ? v : parseFloat(String(v));
  return Number.isFinite(n) ? n : undefined;
}

type RootObj = { path?: string | null; type?: string | null; source?: string | null };
type RootLike = string | RootObj;

export function normalizeRoots(input: unknown): string[] {
  const arr = Array.isArray(input) ? input : [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of arr) {
    let path = '';
    if (typeof raw === 'string') {
      path = raw;
    } else if (raw && typeof raw === 'object') {
      const maybe = (raw as RootObj).path;
      if (typeof maybe === 'string') path = maybe;
    }
    path = (path || '').trim();
    if (!path) continue;
    if (seen.has(path)) continue;
    seen.add(path);
    out.push(path);
  }
  return out;
}

export function normalizeRootTypes(input: unknown): Record<string, string> {
  const arr = Array.isArray(input) ? input : [];
  const out: Record<string, string> = {};
  for (const raw of arr) {
    if (!raw || typeof raw !== 'object') continue;
    const path = typeof (raw as any).path === 'string' ? String((raw as any).path).trim() : '';
    if (!path) continue;
    const t = typeof (raw as any).type === 'string' ? String((raw as any).type).trim().toLowerCase() : '';
    if (!t || t === 'unknown') continue;
    out[path] = t;
  }
  return out;
}

export function buildRootTypesPayload(rootTypes: Record<string, string> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(rootTypes || {})) {
    const path = String(k || '').trim();
    if (!path) continue;
    const t = String(v || '').trim().toLowerCase();
    if (!t || t === 'auto' || t === 'unknown') continue;
    // normalize aliases
    if (t === 'tv' || t === 'show' || t === 'shows') out[path] = 'series';
    else out[path] = t;
  }
  return out;
}

function mapMediaType(t: string | undefined): 'movie' | 'series' | 'anime' {
  const s = (t || '').toLowerCase();
  if (s.includes('anime')) return 'anime';
  if (s.includes('series') || s.includes('tv') || s.includes('show')) return 'series';
  return 'movie';
}

function resolveAiringState(
  provider: string,
  mediaType: 'movie' | 'series' | 'anime',
  metadata: any,
): { isAiring: boolean; airingStatus?: string } {
  if (mediaType === 'movie') return { isAiring: false };

  const raw =
    (typeof metadata?.status === 'string' && metadata.status.trim())
      ? metadata.status.trim()
      : undefined;
  const status = (raw || '').toLowerCase();
  if (!status) return { isAiring: false };

  // Jikan: "Currently Airing", "Finished Airing"
  if (provider === 'jikan') {
    return { isAiring: status === 'currently airing' || status === 'airing', airingStatus: raw };
  }
  // Kitsu: "current", "finished", "tba", ...
  if (provider === 'kitsu') {
    return { isAiring: status === 'current' || status === 'ongoing' || status === 'airing', airingStatus: raw };
  }
  // TMDB TV: "Returning Series", "In Production", ...
  if (provider === 'tmdb') {
    return { isAiring: status === 'returning series' || status === 'in production' || status === 'continuing', airingStatus: raw };
  }

  // Generic fallback for unknown providers.
  const generic = new Set([
    'currently airing',
    'airing',
    'current',
    'ongoing',
    'returning series',
    'in production',
    'continuing',
  ]);
  return { isAiring: generic.has(status), airingStatus: raw };
}

function mapBackendItem(it: any): MediaItem {
  const metaList = Array.isArray(it.metadata) ? it.metadata : [];
  let metaEntry = it.primary_metadata || null;
  if (!metaEntry && metaList.length) {
    metaEntry = metaList.find((m: any) => m && m.provider) || metaList[0];
  }
  let metaDataObj: any = {};
  try {
    if (metaEntry) {
      if (typeof metaEntry.data === 'string') {
        metaDataObj = JSON.parse(metaEntry.data);
      } else {
        metaDataObj = metaEntry.data || {};
      }
    }
  } catch (e) {
    metaDataObj = {};
  }

  const posterUrl = (typeof it.poster_url === 'string' && it.poster_url) ? it.poster_url : `/api/media/${it.id}/poster`;
  const files = Array.isArray(it.files) ? it.files : [];
  const primaryPath = files.length ? files[0].path : (it.canonical_path || '');

  const uiLang = getUiLang();
  const provider = String(metaEntry?.provider || metaDataObj?.provider || it.provider || '').toLowerCase();
  const i18nObj = provider === 'tmdb' ? (metaDataObj?.raw?.i18n || metaDataObj?.i18n || null) : null;
  const localized = i18nObj && typeof i18nObj === 'object' ? (i18nObj[uiLang] || i18nObj.en || i18nObj.es || null) : null;

  const rating = _toNumber(it.rating ?? metaDataObj?.score ?? metaDataObj?.rating ?? metaDataObj?.vote_average ?? metaDataObj?.rating?.value);
  const contentRating = (typeof it.content_rating === 'string' && it.content_rating.trim())
    ? it.content_rating.trim()
    : (typeof metaDataObj?.content_rating === 'string' ? metaDataObj.content_rating.trim() : undefined);
  const overview =
    it?.synopsis_localized ??
    localized?.overview ??
    localized?.synopsis ??
    (provider === 'tmdb' ? (metaDataObj?.overview ?? metaDataObj?.synopsis ?? metaDataObj?.description) : undefined);
  const release = it.release_year ?? metaDataObj?.year ?? (metaDataObj?.release_date ? String(metaDataObj.release_date).slice(0, 4) : undefined);
  const year = release ? _toNumber(release) : undefined;

  const genres = Array.isArray(it.genres)
    ? it.genres
    : (Array.isArray(localized?.genres) ? localized.genres : (Array.isArray(metaDataObj?.genres) ? metaDataObj.genres : (metaDataObj?.genres ? [metaDataObj.genres] : undefined)));

  const cast = Array.isArray(it.cast)
    ? it.cast
    : (Array.isArray(metaDataObj?.cast) ? metaDataObj.cast : undefined);

  const resolvedTitle =
    (typeof it?.title_localized === 'string' && it.title_localized.trim())
      ? it.title_localized.trim()
      : (typeof it?.title_en === 'string' && it.title_en.trim())
        ? it.title_en.trim()
      : (localized?.title || it.title || it.base_title || '');
  const mediaType = mapMediaType(it.media_type);
  const backendAiringStatus =
    (typeof it.airing_status === 'string' && it.airing_status.trim())
      ? it.airing_status.trim()
      : undefined;
  const backendAiring = (typeof it.is_airing === 'boolean') ? it.is_airing : undefined;
  const airing =
    backendAiring !== undefined
      ? { isAiring: backendAiring, airingStatus: backendAiringStatus }
      : resolveAiringState(provider, mediaType, metaDataObj);

  const manualBackdrop =
    (typeof it.backdrop_path === 'string' && it.backdrop_path.trim())
      ? it.backdrop_path
      : undefined;
  const remoteBackdrop =
    (typeof (it as any).backdrop_url === 'string' && String((it as any).backdrop_url).trim())
      ? String((it as any).backdrop_url).trim()
      : undefined;
  const _isLocalAsset = (u?: string) => !!(u && u.trim().startsWith('/media-assets/'));
  const resolvedBackdrop =
    (_isLocalAsset(manualBackdrop) ? manualBackdrop : undefined) ||
    (_isLocalAsset(remoteBackdrop) ? remoteBackdrop : undefined) ||
    manualBackdrop ||
    remoteBackdrop ||
    metaDataObj?.backdrop_path ||
    undefined;

  const item: MediaItem = {
    id: String(it.id ?? ''),
    title: resolvedTitle,
    path: primaryPath,
    type: mediaType,
    year: year as number | undefined,
    duration: _toNumber(metaDataObj?.duration) as number | undefined,
    // Poster URL used by the UI
    posterPath: posterUrl,
    thumbnailUrl: posterUrl,
    backdropUrl: resolvedBackdrop,
    overview,
    rating: rating as number | undefined,
    contentRating: contentRating || undefined,
    genre: genres,
    addedAt: it.created_at || new Date().toISOString(),
    // quick-access enriched fields
    malId: it.mal_id ?? metaDataObj?.mal_id ?? undefined,
    tmdbId: it.tmdb_id ?? metaDataObj?.tmdb_id ?? metaDataObj?.id ?? undefined,
    isAnimated: (it.is_animated ?? metaDataObj?.is_animated) ? true : false,
    origin: it.origin ?? metaDataObj?.origin ?? undefined,
    releaseYear: it.release_year ?? metaDataObj?.release_year ?? year as number | undefined,
    runtime: _toNumber(it.runtime ?? metaDataObj?.runtime) as number | undefined,
    cast,
    is_identified: (it as any).is_identified ?? undefined,
    seriesId: it.series_id ?? (metaDataObj?.series_id ?? undefined),
    seasonNumber: _toNumber(it.season_number) as number | undefined,
    isAiring: airing.isAiring,
    airingStatus: airing.airingStatus,
    };

  // Attach raw metadata and files so UI can render details and episode lists
  try {
    // Preserve normalized metadata and provider raw payload when available
    if (metaEntry) {
      (item as any).rawMetadata = metaDataObj || {};
    }
    if (files.length) {
      (item as any).files = files.map((f: any) => ({ filename: f.filename || f.filename, path: f.path, size: f.size, mtime: f.mtime, index: f.index ?? f.file_index }));
    }
  } catch (e) {
    // ignore
  }

  return item;
}

async function fetchWithFallback(input: string, init?: RequestInit) {
  // Accept full URL or /api-relative path
  const tryUrls: string[] = [];
  const directBases = _getDirectApiBases();
  if (/^https?:\/\//.test(input)) {
    tryUrls.push(input);
  } else if (input.startsWith('/api')) {
    tryUrls.push(input);
    for (const b of directBases) tryUrls.push(input.replace('/api', b));
  } else {
    // relative path: prepend API_BASE
    tryUrls.push(`${API_BASE}${input.startsWith('/') ? input : `/${input}`}`);
    for (const b of directBases) tryUrls.push(`${b}${input.startsWith('/') ? input : `/${input}`}`);
  }

  let lastErr: any = null;
  for (const u of tryUrls) {
    try {
      const res = await fetch(u, init as any);
      if (res.ok) return res;
      // On 404 try next fallback URL
      lastErr = new Error(`HTTP ${res.status} ${res.statusText}`);
      if (res.status === 404) continue;
      // For other non-ok statuses, still return the response so callers can handle it
      return res;
    } catch (e) {
      lastErr = e;
      // try next fallback URL
      continue;
    }
  }
  throw lastErr;
}

export const MediaService = {
  // Helpers for legacy callers (SettingsView) that assume these are methods.
  getTelemetrySseUrls,
  getStatusWsUrls,

  async getAll(page: number = 1, limit: number = 50, query?: string, types?: string[], isKidsSafe?: boolean): Promise<{ items: MediaItem[]; total: number }> {
    const skip = Math.max(0, (page - 1) * limit);
    try {
      const queryParam = query ? `&search=${encodeURIComponent(query)}` : '';
      const typesParam = (types && types.length) ? `&types=${encodeURIComponent(types.join(','))}` : '';
      const kidsParam = isKidsSafe ? `&is_kids_safe=true` : '';
      const response = await fetchWithFallback(`${API_BASE}/media?skip=${skip}&limit=${limit}&lite=true${queryParam}${typesParam}${kidsParam}`);
      if (!response.ok) throw new Error('Network response was not ok');
      const data = await response.json();
      const items = (data.items || []).map(mapBackendItem);
      return { items, total: data.total || items.length };
    } catch (error) {
      console.warn('Backend unavailable, using mock data', error);
      await delay(300);
      let items = [...MOCK_MEDIA];
      if (types && types.length) {
        const lower = new Set(types.map(t => String(t).toLowerCase()));
        items = items.filter(i => lower.has(String(i.type).toLowerCase()));
      }
      if (query) {
        const lowerQuery = query.toLowerCase();
        items = items.filter(item => item.title.toLowerCase().includes(lowerQuery) || (item.overview && item.overview.toLowerCase().includes(lowerQuery)));
      }
      const total = items.length;
      const paged = items.slice(skip, skip + limit);
      return { items: paged as MediaItem[], total };
    }
  },

  async getLibraryOverview(groupBy: string = 'library_type', sample: number = 6): Promise<any> {
    try {
      const response = await fetchWithFallback(`${API_BASE}/library/overview?group_by=${encodeURIComponent(groupBy)}&sample=${encodeURIComponent(String(sample))}`);
      if (!response.ok) throw new Error('Failed to fetch library overview');
      const data = await response.json();
      return data;
    } catch (error) {
      console.warn('Failed to fetch library overview', error);
      return { groups: [] };
    }
  },

  async getNoMatchItems(page: number = 1, limit: number = 50): Promise<{ items: MediaItem[]; total: number }> {
    const skip = Math.max(0, (page - 1) * limit);
    try {
      const response = await fetchWithFallback(`${API_BASE}/media?skip=${skip}&limit=${limit}&status=NO_MATCH`, {
        headers: adminHeadersAny(),
      });
      if (!response.ok) throw new Error('Failed to fetch no-match items');
      const data = await response.json();
      const items = (data.items || []).map(mapBackendItem);
      return { items, total: data.total || items.length };
    } catch (e) {
      console.warn('Failed to fetch no-match items', e);
      return { items: [], total: 0 };
    }
  },

  async searchSuggest(query: string, limit: number = 20, types?: string[]): Promise<{ items: MediaItem[]; total: number }> {
    const q = String(query || '').trim();
    if (!q) return { items: [], total: 0 };
    const typesParam = (types && types.length) ? `&types=${encodeURIComponent(types.join(','))}` : '';
    const response = await fetchWithFallback(`${API_BASE}/media/search-suggest?query=${encodeURIComponent(q)}&limit=${limit}${typesParam}`);
    if (!response.ok) throw new Error('Failed to fetch search suggestions');
    const data = await response.json();
    const items = (data.items || []).map(mapBackendItem);
    return { items, total: data.total || items.length };
  },

  async getById(itemId: number): Promise<MediaItem> {
    const response = await fetchWithFallback(`${API_BASE}/media/${itemId}`);
    if (!response.ok) throw new Error('Failed to fetch media item');
    const data = await response.json();
    return mapBackendItem(data);
  },

  async omitMediaItem(itemId: number): Promise<{ ok: boolean; detail?: string }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/media/${itemId}/omit`, {
        method: 'POST',
        headers: adminHeadersAny(),
      });
      let data: any = null;
      try { data = await res.json(); } catch { data = null; }
      if (!res.ok) return { ok: false, detail: data?.detail || `HTTP ${res.status}` };
      return { ok: true };
    } catch (e) {
      return { ok: false, detail: String(e) };
    }
  },

  async getEpisodeSeasons(itemId: number, opts?: { includeRelated?: boolean }): Promise<any> {
    const includeRelated = opts?.includeRelated !== undefined ? !!opts.includeRelated : true;
    const response = await fetchWithFallback(`${API_BASE}/media/${itemId}/episode-seasons?include_related=${includeRelated ? 'true' : 'false'}`);
    if (!response.ok) throw new Error('Failed to fetch episode seasons');
    return await response.json();
  },

  async enrichOne(itemId: number): Promise<{ ok: boolean; detail?: string }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/media/${itemId}/enrich`, { method: 'POST', headers: adminHeadersAny() });
      let data: any = null;
      try { data = await res.json(); } catch (e) { data = null; }
      if (!res.ok) return { ok: false, detail: data?.detail || `HTTP ${res.status}` };
      if (!data?.ok) return { ok: false, detail: data?.detail || 'No se pudieron actualizar los metadatos.' };
      return { ok: true };
    } catch (e) {
      return { ok: false, detail: String(e) };
    }
  },

  getPosterUrl(itemId: number): string {
    return `${API_BASE}/media/${itemId}/poster`;
  },

  getStreamUrlFromPath(path: string): string {
    return `${API_BASE}/stream?path=${encodeURIComponent(path)}`;
  },

  async startScan(): Promise<{ status: string }> {
    try {
      const response = await fetchWithFallback(`${API_BASE}/scan`, { method: 'POST', headers: adminHeadersAny() });
      let data: any = null;
      try {
        data = await response.json();
      } catch (e) {
        data = null;
      }
      return { status: data?.status || (response.ok ? 'accepted' : 'error'), detail: data?.detail || null, raw: data } as any;
    } catch (error) {
      console.warn('Scan failed, returning mock status', error);
      return { status: 'mock_failed', detail: String(error) } as any;
    }
  },

  async getScanStatus(): Promise<ScanStatus> {
    try {
      const response = await fetchWithFallback(`${API_BASE}/scan/status`, { headers: adminHeadersAny() });
      if (!response.ok) throw new Error('Failed to fetch scan status');
      const data = await response.json();
      // Ensure enrichment is present as returned by backend
      const out: any = {
        scanning: !!data.scanning || data.status === 'scanning' || data.status === 'queued' || false,
        progress: typeof data.progress === 'number' ? data.progress : (data.total && data.processed ? Math.round((data.processed / data.total) * 100) : 0),
        currentFile: data.current || data.currentFile || null,
        total: data.total,
        processed: data.processed,
        queued: data.queued,
        status: data.status,
        enrichment: data.enrichment || null,
        phase: data.phase,
        last_item: data.last_item,
        root_counts: data.root_counts || null,
        watch: data.watch || null,
        titles_total: data.titles_total ?? data.titlesTotal,
      };
      return out as ScanStatus;
    } catch (error) {
      return { scanning: false, progress: 100 } as ScanStatus;
    }
  },

  async getScanItems(limit: number = 200, offset: number = 0, root?: string | null): Promise<{ total: number; items: Array<{ id: number | string; title?: string | null; path?: string | null; media_root?: string | null; media_type?: string | null }> }> {
    const qs: string[] = [];
    if (typeof limit === 'number') qs.push(`limit=${encodeURIComponent(String(limit))}`);
    if (typeof offset === 'number') qs.push(`offset=${encodeURIComponent(String(offset))}`);
    if (root) qs.push(`root=${encodeURIComponent(root)}`);
    const url = qs.length ? `${API_BASE}/scan/items?${qs.join('&')}` : `${API_BASE}/scan/items`;
    const res = await fetchWithFallback(url, { headers: adminHeadersAny() });
    if (!res.ok) throw new Error('Failed to fetch scan items');
    const data = await res.json();
    return { total: Number(data?.total || 0), items: Array.isArray(data?.items) ? data.items : [] };
  },

  async getEnrichStatus(): Promise<any> {
    try {
      const response = await fetchWithFallback(`${API_BASE}/enrich/status`, { headers: adminHeadersAny() });
      if (!response.ok) throw new Error('Failed to fetch enrich status');
      const data = await response.json();
      return data;
    } catch (e) {
      return { running: false, total: 0, pending: 0, enriched: 0 };
    }
  },

  async searchEntities(query: string, limit: number = 50, offset: number = 0): Promise<any> {
    const q = String(query || '').trim();
    const response = await fetchWithFallback(`${API_BASE}/admin/entities/search?q=${encodeURIComponent(q)}&limit=${limit}&offset=${offset}`, {
      headers: adminHeadersAny(),
    });
    if (!response.ok) throw new Error('Failed to search entities');
    return await response.json();
  },

  async deepPurgeEntity(itemId: number): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/admin/entities/${itemId}/deep-purge`, {
      method: 'POST',
      headers: adminHeadersAny(),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data?.detail || 'Failed to purge');
    return data;
  },

  async rematchEntity(itemId: number, payload: any): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/admin/entities/${itemId}/rematch`, {
      method: 'POST',
      headers: { ...(adminHeadersAny() || {}), 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data?.detail || 'Failed to rematch');
    return data;
  },

  async getConflicts(kind: string = 'all'): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/admin/conflicts?kind=${encodeURIComponent(kind)}`, {
      headers: adminHeadersAny(),
    });
    if (!response.ok) throw new Error('Failed to fetch conflicts');
    return await response.json();
  },

  async mergeConflicts(keepId: number, dropId: number): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/admin/conflicts/merge`, {
      method: 'POST',
      headers: { ...(adminHeadersAny() || {}), 'Content-Type': 'application/json' },
      body: JSON.stringify({ keep_id: keepId, drop_id: dropId }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data?.detail || 'Failed to merge');
    return data;
  },

  async fullSystemReset(confirm: string): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/admin/system/reset`, {
      method: 'POST',
      headers: { ...(adminHeadersAny() || {}), 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data?.detail || 'Failed to reset');
    return data;
  },

  async clearScanQueue(): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/admin/queue/clear`, {
      method: 'POST',
      headers: adminHeadersAny(),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data?.detail || 'Failed to clear queue');
    return data;
  },

  async manualSearchTmdb(query: string, language: string): Promise<any> {
    const q = encodeURIComponent(query || '');
    const lang = encodeURIComponent(language || 'es-MX');
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/tmdb/search?query=${q}&language=${lang}`, {
      headers: adminHeaders(),
    });
    if (!response.ok) throw new Error('Failed to search TMDB');
    return response.json();
  },

  async manualSearchJikan(query: string): Promise<any> {
    const q = encodeURIComponent(query || '');
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/jikan/search?query=${q}`, {
      headers: adminHeaders(),
    });
    if (!response.ok) throw new Error('Failed to search Jikan');
    return response.json();
  },

  async manualSearchKitsu(query: string): Promise<any> {
    const q = encodeURIComponent(query || '');
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/kitsu/search?query=${q}`, {
      headers: adminHeaders(),
    });
    if (!response.ok) throw new Error('Failed to search Kitsu');
    return response.json();
  },

  async manualTmdbDetails(tmdbId: string, mediaType: string, language: string): Promise<any> {
    const id = encodeURIComponent(tmdbId);
    const mt = encodeURIComponent(mediaType || 'tv');
    const lang = encodeURIComponent(language || 'es-MX');
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/tmdb/${id}/details?media_type=${mt}&language=${lang}`, {
      headers: adminHeaders(),
    });
    if (!response.ok) throw new Error('Failed to fetch TMDB details');
    return response.json();
  },

  async manualJikanDetails(malId: string): Promise<any> {
    const id = encodeURIComponent(malId);
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/jikan/${id}/details`, {
      headers: adminHeaders(),
    });
    if (!response.ok) throw new Error('Failed to fetch Jikan details');
    return response.json();
  },

  async manualKitsuDetails(kitsuId: string): Promise<any> {
    const id = encodeURIComponent(kitsuId);
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/kitsu/${id}/details`, {
      headers: adminHeaders(),
    });
    if (!response.ok) throw new Error('Failed to fetch Kitsu details');
    return response.json();
  },

  async manualTmdbSeasons(tmdbId: string, language: string): Promise<any> {
    const id = encodeURIComponent(tmdbId);
    const lang = encodeURIComponent(language || 'es-MX');
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/tmdb/${id}/seasons?language=${lang}`, {
      headers: adminHeaders(),
    });
    if (!response.ok) throw new Error('Failed to fetch TMDB seasons');
    return response.json();
  },

  async manualTmdbSeasonDetails(tmdbId: string, seasonNumber: number, language: string): Promise<any> {
    const id = encodeURIComponent(tmdbId);
    const lang = encodeURIComponent(language || 'es-MX');
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/tmdb/${id}/season/${seasonNumber}?language=${lang}`, {
      headers: adminHeaders(),
    });
    if (!response.ok) throw new Error('Failed to fetch TMDB season');
    return response.json();
  },

  async manualLocalSeasons(itemId: number): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/${encodeURIComponent(String(itemId))}/local-seasons`, {
      headers: adminHeaders(),
    });
    if (!response.ok) throw new Error('Failed to fetch local seasons');
    return response.json();
  },

  async manualApplyMapping(itemId: number, payload: any): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/manual-mapping/${itemId}/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(adminHeaders() || {}) },
      body: JSON.stringify(payload || {}),
    });
    if (!response.ok) throw new Error('Failed to apply manual mapping');
    return response.json();
  },

  async getRecommendations(limit: number = 20, profileId?: string): Promise<any> {
    const pid = profileId ? `&profile_id=${encodeURIComponent(profileId)}` : '';
    const response = await fetchWithFallback(`${API_BASE}/media/recommendations?limit=${encodeURIComponent(String(limit))}${pid}`, {
      headers: profileHeaders(profileId),
    });
    if (!response.ok) throw new Error('Failed to fetch recommendations');
    return response.json();
  },

  async getInProgress(limit: number = 12, profileId?: string): Promise<any> {
    const pid = profileId ? `&profile_id=${encodeURIComponent(profileId)}` : '';
    const response = await fetchWithFallback(`${API_BASE}/media/in-progress?limit=${encodeURIComponent(String(limit))}${pid}`, {
      headers: profileHeaders(profileId),
    });
    if (!response.ok) throw new Error('Failed to fetch in-progress items');
    return response.json();
  },

  async recordPlay(itemId: number, profileId?: string): Promise<any> {
    const pid = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : '';
    const response = await fetchWithFallback(`${API_BASE}/media/${itemId}/play${pid}`, {
      method: 'POST',
      headers: profileHeaders(profileId),
    });
    if (!response.ok) throw new Error('Failed to record play');
    return response.json();
  },

  async getPlaybackProgress(itemId: number, profileId?: string): Promise<any> {
    const pid = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : '';
    const response = await fetchWithFallback(`${API_BASE}/media/${itemId}/progress${pid}`, {
      headers: profileHeaders(profileId),
    });
    if (!response.ok) throw new Error('Failed to fetch playback progress');
    return response.json();
  },

  async savePlaybackProgress(itemId: number, payload: { position?: number; duration?: number; path?: string | null }, profileId?: string): Promise<any> {
    const pid = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : '';
    const response = await fetchWithFallback(`${API_BASE}/media/${itemId}/progress${pid}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(profileHeaders(profileId) || {}) },
      body: JSON.stringify(payload || {}),
    });
    if (!response.ok) throw new Error('Failed to save playback progress');
    return response.json();
  },

  async getUserList(profileId: string): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/user-list`, {
      headers: profileHeaders(profileId),
    });
    if (!response.ok) throw new Error('Failed to fetch user list');
    return response.json();
  },

  async getUserListItem(itemId: number, profileId: string): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/user-list/${itemId}`, {
      headers: profileHeaders(profileId),
    });
    if (!response.ok) throw new Error('Failed to fetch user list item');
    return response.json();
  },

  async toggleUserListItem(itemId: number, profileId: string): Promise<any> {
    const response = await fetchWithFallback(`${API_BASE}/user-list/${itemId}/toggle`, {
      method: 'POST',
      headers: profileHeaders(profileId),
    });
    if (!response.ok) throw new Error('Failed to toggle user list');
    return response.json();
  },

  async startEnrichment(): Promise<any> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/enrich/start`, { method: 'POST', headers: adminHeadersAny() });
      let data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      return { ok: res.ok, data };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  },

  async backfillEpisodes(limitSeasons?: number): Promise<any> {
    try {
      const url = typeof limitSeasons === 'number'
        ? `${API_BASE}/enrich/backfill-episodes?limit_seasons=${encodeURIComponent(String(limitSeasons))}`
        : `${API_BASE}/enrich/backfill-episodes`;
      const res = await fetchWithFallback(url, { method: 'POST', headers: adminHeadersAny() });
      let data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      return { ok: res.ok, data };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  },

  async backfillImages(limitItems?: number): Promise<any> {
    try {
      const url = typeof limitItems === 'number'
        ? `${API_BASE}/enrich/backfill-images?limit_items=${encodeURIComponent(String(limitItems))}`
        : `${API_BASE}/enrich/backfill-images`;
      const res = await fetchWithFallback(url, { method: 'POST', headers: adminHeadersAny() });
      let data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      return { ok: res.ok, data };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  },

  async resetNoMatch(): Promise<any> {
    // Back-compat: default behavior (no options).
    return this.resetNoMatchWithOptions();
  },

  async resetNoMatchWithOptions(opts?: { animeProvider?: 'jikan' | 'kitsu' | 'tmdb' | 'auto'; start?: boolean }): Promise<any> {
    try {
      const qp: string[] = [];
      if (opts?.animeProvider) qp.push(`anime_provider=${encodeURIComponent(String(opts.animeProvider))}`);
      if (typeof opts?.start === 'boolean') qp.push(`start=${encodeURIComponent(String(opts.start))}`);
      const url = qp.length ? `${API_BASE}/enrich/reset-no-match?${qp.join('&')}` : `${API_BASE}/enrich/reset-no-match`;
      const res = await fetchWithFallback(url, { method: 'POST', headers: adminHeadersAny() });
      let data = null;
      try { data = await res.json(); } catch { data = null; }
      return { ok: res.ok, data };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  },

  async resetError(opts?: { animeProvider?: 'jikan' | 'kitsu' | 'tmdb' | 'auto'; start?: boolean }): Promise<any> {
    try {
      const qp: string[] = [];
      if (opts?.animeProvider) qp.push(`anime_provider=${encodeURIComponent(String(opts.animeProvider))}`);
      if (typeof opts?.start === 'boolean') qp.push(`start=${encodeURIComponent(String(opts.start))}`);
      const url = qp.length ? `${API_BASE}/enrich/reset-error?${qp.join('&')}` : `${API_BASE}/enrich/reset-error`;
      const res = await fetchWithFallback(url, { method: 'POST', headers: adminHeadersAny() });
      let data = null;
      try { data = await res.json(); } catch { data = null; }
      return { ok: res.ok, data };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  },

  async getDrives(): Promise<Drive[]> {
    try {
      const response = await fetchWithFallback(`${API_BASE}/drives`, { headers: adminHeadersAny() });
      if (!response.ok) throw new Error('Failed to fetch drives');
      const data = await response.json();
      const drivesData: any[] = data.drives || [];
      return drivesData.map(d => ({ path: d.path, label: d.label || (d.path || '').split(/[\\/]/).filter(Boolean).pop() || d.path, totalSpace: d.totalSpace || 0, freeSpace: d.freeSpace || 0 }));
    } catch (error) {
      console.warn('Failed to fetch drives, using fallback', error);
      return [
        { path: 'C:\\', label: 'C', totalSpace: 0, freeSpace: 0 },
        { path: 'D:\\', label: 'D', totalSpace: 0, freeSpace: 0 },
      ];
    }
  },
  async getRoots(): Promise<string[]> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/roots`, { headers: adminHeadersAny() });
      if (!res.ok) throw new Error('Failed to fetch roots');
      const data = await res.json();
      return normalizeRoots(data.roots);
    } catch (e) {
      console.warn('Failed to fetch roots', e);
      return [];
    }
  },

  async getRootsDetailed(): Promise<RootObj[]> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/roots`, { headers: adminHeadersAny() });
      if (!res.ok) throw new Error('Failed to fetch roots');
      const data = await res.json();
      return Array.isArray(data.roots) ? data.roots : [];
    } catch (e) {
      console.warn('Failed to fetch roots (detailed)', e);
      return [];
    }
  },

  async addRoot(path: string, type?: string): Promise<RootObj[]> {
    try {
      let t = String(type || '').trim().toLowerCase();
      if (t === 'tv' || t === 'show' || t === 'shows') t = 'series';
      if (!t || t === 'auto' || t === 'unknown') t = '';
      const res = await fetchWithFallback(`${API_BASE}/roots`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(adminHeadersAny() || {}) },
        body: JSON.stringify(t ? { path, type: t } : { path }),
      });
      if (!res.ok) throw new Error('Failed to add root');
      const data = await res.json();
      return Array.isArray(data.roots) ? data.roots : [];
    } catch (e) {
      console.warn('Failed to add root', e);
      return [];
    }
  },

  async removeRoot(path: string): Promise<RootObj[]> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/roots`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json', ...(adminHeadersAny() || {}) },
        body: JSON.stringify({ path }),
      });
      if (!res.ok) throw new Error('Failed to remove root');
      const data = await res.json();
      return Array.isArray(data.roots) ? data.roots : [];
    } catch (e) {
      console.warn('Failed to remove root', e);
      return [];
    }
  },

  async updateRootType(path: string, type: string): Promise<RootObj[]> {
    const t = String(type || '').trim().toLowerCase();
    const tNorm = (!t || t === 'auto') ? 'unknown' : (t === 'tv' ? 'series' : t);
    const res = await fetchWithFallback(`${API_BASE}/roots`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...(adminHeadersAny() || {}) },
      body: JSON.stringify({ path, type: tNorm }),
    });
    if (!res.ok) throw new Error('Failed to update root');
    const data = await res.json();
    return Array.isArray(data.roots) ? data.roots : [];
  },
  async getAppConfig(): Promise<any> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/app-config`, {
        headers: adminHeadersAny(),
      });
      if (!res.ok) {
        // Si es 404, el endpoint no existe, retornar objeto vacío
        if (res.status === 404) {
          console.warn('app-config endpoint not found (404), returning empty config');
          return {
            setupComplete: false,
            profiles: [],
            metadata: {},
            media_roots: [],
            loose_movie_roots: []
          };
        }
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }
      const data = await res.json();
      console.log('App config loaded:', data);
      try {
        setTelemetryToken((data as any)?.telemetry_token);
      } catch {
        // ignore
      }
      return data;
    } catch (e) {
      console.warn('Failed to fetch app config, using default:', e);
      // Retornar configuración por defecto
        return {
          setupComplete: false,
          profiles: [],
          metadata: {
            moviesProvider: 'tmdb',
            animeProvider: 'kitsu',
            language: 'en-US',
            downloadImages: true,
            fetchCast: true
          },
        media_roots: [],
        loose_movie_roots: []
      };
    }
  },

  async getLocalizeStatus(): Promise<any> {
    const res = await fetchWithFallback(`${API_BASE}/localize/status`, { headers: adminHeadersAny() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  async startLocalize(opts?: { limit_series?: number; limit_seasons?: number }): Promise<any> {
    const qs: string[] = [];
    if (opts?.limit_series != null) qs.push(`limit_series=${encodeURIComponent(String(opts.limit_series))}`);
    if (opts?.limit_seasons != null) qs.push(`limit_seasons=${encodeURIComponent(String(opts.limit_seasons))}`);
    const url = `${API_BASE}/localize/start${qs.length ? `?${qs.join('&')}` : ''}`;
    const res = await fetchWithFallback(url, { method: 'POST', headers: adminHeadersAny() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  async stopLocalize(): Promise<any> {
    const res = await fetchWithFallback(`${API_BASE}/localize/stop`, { method: 'POST', headers: adminHeadersAny() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  async runAnimeTranslations(opts?: { limit_items?: number; missing_only?: boolean }): Promise<any> {
    const qs: string[] = [];
    if (opts?.limit_items != null) qs.push(`limit_items=${encodeURIComponent(String(opts.limit_items))}`);
    if (opts?.missing_only != null) qs.push(`missing_only=${encodeURIComponent(String(!!opts.missing_only))}`);
    const url = `${API_BASE}/localize/anime-translations${qs.length ? `?${qs.join('&')}` : ''}`;
    const res = await fetchWithFallback(url, { method: 'POST', headers: adminHeadersAny() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  async getAnimeNormalizeStatus(): Promise<any> {
    const res = await fetchWithFallback(`${API_BASE}/anime-normalize/status`, { headers: adminHeadersAny() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  async startAnimeNormalize(opts?: { limit_series?: number; force?: boolean }): Promise<any> {
    const qs: string[] = [];
    if (opts?.limit_series != null) qs.push(`limit_series=${encodeURIComponent(String(opts.limit_series))}`);
    if (opts?.force != null) qs.push(`force=${encodeURIComponent(String(!!opts.force))}`);
    const url = `${API_BASE}/anime-normalize/start${qs.length ? `?${qs.join('&')}` : ''}`;
    const res = await fetchWithFallback(url, { method: 'POST', headers: adminHeadersAny() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  async stopAnimeNormalize(): Promise<any> {
    const res = await fetchWithFallback(`${API_BASE}/anime-normalize/stop`, { method: 'POST', headers: adminHeadersAny() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  async saveAppConfig(cfg: any): Promise<any> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/app-config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(adminHeadersAny() || {}) },
        body: JSON.stringify(cfg),
      });

      if (!res.ok) {
        if (res.status === 404) {
          console.warn('app-config endpoint not found (404), config not saved');
          return cfg; // Retornar la configuración como si se hubiera guardado
        }
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }

      return await res.json();
    } catch (e) {
      console.warn('Failed to save app config:', e);
      // En desarrollo, podemos "simular" que se guardó
      return cfg;
    }
  },
  
  async saveCredentials(payload: any): Promise<any> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/credentials`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(adminHeadersAny() || {}) },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error('Failed to save credentials');
      return await res.json();
    } catch (e) {
      console.warn('Failed to save credentials', e);
      return { saved: false };
    }
  },

  async adminLogin(pin: string): Promise<{ ok: boolean; token?: string; expires_in?: number; detail?: string }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/admin/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, detail: data?.detail || 'invalid' };
      const token = String(data?.token || '');
      if (token) {
        try { localStorage.setItem('arcanea_admin_token', token); } catch { /* ignore */ }
      }
      return { ok: true, token, expires_in: data?.expires_in };
    } catch (e) {
      return { ok: false, detail: String(e) };
    }
  },

  async getAdminStatus(): Promise<{ pin_required: boolean }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/admin/status`);
      if (!res.ok) throw new Error('Failed to fetch admin status');
      const data = await res.json();
      return { pin_required: !!data?.pin_required };
    } catch (e) {
      return { pin_required: false };
    }
  },

  async adminLogout(): Promise<{ ok: boolean }> {
    const token = getAdminToken();
    try {
      if (token) {
        await fetchWithFallback(`${API_BASE}/admin/logout`, {
          method: 'POST',
          headers: { 'X-Arcanea-Admin-Token': token },
        });
      }
    } catch {
      // ignore
    }
    try { localStorage.removeItem('arcanea_admin_token'); } catch { /* ignore */ }
    return { ok: true };
  },

  async getCredentialsStatus(): Promise<{ tmdb_configured: boolean }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/credentials`, { headers: adminHeadersAny() });
      if (!res.ok) throw new Error('Failed to fetch credentials status');
      const data = await res.json();
      return data;
    } catch (e) {
      return { tmdb_configured: false };
    }
  },
  async getCredentialsCheck(): Promise<{ ok: boolean; detail?: string }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/credentials/check`, { headers: adminHeadersAny() });
      if (!res.ok) throw new Error('Failed to check credentials');
      const data = await res.json();
      return data;
    } catch (e) {
      return { ok: false, detail: String(e) };
    }
  },
  async checkCredentials(payload: any): Promise<{ ok: boolean; detail?: string }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/credentials/check`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(adminHeadersAny() || {}) },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error('Failed to validate credentials');
      const data = await res.json();
      return data;
    } catch (e) {
      return { ok: false, detail: String(e) };
    }
  },
  async pickRoots(multiple: boolean = false): Promise<string[]> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/roots/pick`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(adminHeadersAny() || {}) },
        body: JSON.stringify({ multiple }),
      });
      if (!res.ok) throw new Error('Failed to pick roots');
      const data = await res.json();
      return normalizeRoots(data.paths);
    } catch (e) {
      console.warn('pickRoots failed', e);
      return [];
    }
  },

  async listDirectories(path?: string | null): Promise<{ path: string | null; parent: string | null; entries: Array<{ name: string; path: string; type: string }> }> {
    try {
      const qp = path ? `?path=${encodeURIComponent(path)}` : '';
      const res = await fetchWithFallback(`${API_BASE}/fs/list${qp}`, { headers: adminHeadersAny() });
      if (!res.ok) throw new Error(`Failed to list directories (${res.status})`);
      const data = await res.json();
      return {
        path: (typeof data?.path === 'string' ? data.path : null),
        parent: (typeof data?.parent === 'string' ? data.parent : null),
        entries: Array.isArray(data?.entries) ? data.entries : [],
      };
    } catch (e) {
      console.warn('listDirectories failed', e);
      return { path: path ?? null, parent: null, entries: [] };
    }
  },

  async getGDriveAuthUrl(): Promise<{ auth_url: string }> {
    const res = await fetchWithFallback(`${API_BASE}/gdrive/auth/url`, { headers: adminHeadersAny() });
    if (!res.ok) throw new Error('Failed to get auth url');
    return res.json();
  },

  async getGDriveStatus(): Promise<{ authenticated: boolean }> {
    const res = await fetchWithFallback(`${API_BASE}/gdrive/status`, { headers: adminHeadersAny() });
    if (!res.ok) throw new Error('Failed to get status');
    return res.json();
  },

  async listGDriveFolders(parentId: string = 'root'): Promise<{ folders: any[] }> {
    const res = await fetchWithFallback(`${API_BASE}/gdrive/folders?parent_id=${encodeURIComponent(parentId)}`, { headers: adminHeadersAny() });
    if (!res.ok) throw new Error('Failed to list folders');
    return res.json();
  },

  async addGDriveRoot(folderId: string, folderName?: string, type?: string): Promise<any> {
    const res = await fetchWithFallback(`${API_BASE}/gdrive/roots`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(adminHeadersAny() || {}) },
      body: JSON.stringify({ folder_id: folderId, folder_name: folderName, type }),
    });
    if (!res.ok) throw new Error('Failed to add gdrive root');
    return res.json();
  },

  async getHealth(): Promise<any> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/health`);
      if (!res.ok) throw new Error('Failed to fetch health');
      return await res.json();
    } catch (e) {
      return { ok: false, detail: String(e) };
    }
  },

  async getLanInfo(): Promise<{ hosts: string[]; preferred_host?: string | null; port?: number; download_url?: string | null }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/system/lan-info`);
      if (!res.ok) throw new Error('Failed to fetch lan info');
      const data = await res.json();
      return {
        hosts: Array.isArray(data?.hosts) ? data.hosts.map((h: any) => String(h)) : [],
        preferred_host: data?.preferred_host ? String(data.preferred_host) : null,
        port: typeof data?.port === 'number' ? data.port : Number(data?.port || 0) || undefined,
        download_url: data?.download_url ? String(data.download_url) : null,
      };
    } catch (e) {
      return { hosts: [], preferred_host: null, port: undefined, download_url: null };
    }
  },

  async getLogsInfo(): Promise<{ path: string; exists: boolean; size: number }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/logs/info`, { headers: adminHeaders() });
      if (!res.ok) throw new Error('Failed to fetch logs info');
      const data = await res.json();
      return {
        path: String(data?.path || ''),
        exists: !!data?.exists,
        size: Number(data?.size || 0),
      };
    } catch (e) {
      return { path: '', exists: false, size: 0 };
    }
  },

  async getLogsTail(lines: number = 200): Promise<{ path: string; lines: string[]; error?: string }> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/logs/tail?lines=${encodeURIComponent(String(lines))}`, { headers: adminHeaders() });
      if (!res.ok) throw new Error('Failed to tail logs');
      const data = await res.json();
      return {
        path: String(data?.path || ''),
        lines: Array.isArray(data?.lines) ? data.lines.map((x: any) => String(x)) : [],
        error: data?.error ? String(data.error) : undefined,
      };
    } catch (e) {
      return { path: '', lines: [], error: String(e) };
    }
  },

  async downloadBackup(): Promise<void> {
    try {
      const res = await fetchWithFallback(`${API_BASE}/backup/export`, { headers: adminHeaders() });
      if (!res.ok) throw new Error('Failed to export backup');
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      const cd = res.headers.get('Content-Disposition') || '';
      const match = /filename=\"?([^\";]+)\"?/i.exec(cd);
      a.href = url;
      a.download = match?.[1] || 'arcanea-backup.zip';
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) {
      console.warn('Failed to export backup', e);
    }
  },
};
