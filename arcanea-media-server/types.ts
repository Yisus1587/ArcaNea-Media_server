export interface MediaItem {
  id: string;
  title: string;
  path: string;
  type: 'movie' | 'series' | 'anime';
  year?: number;
  duration?: number; // in minutes (legacy field; may be unset)
  thumbnailUrl?: string;
  backdropUrl?: string;
  overview?: string;
  synopsis?: string;
  rating?: number;
  contentRating?: string;
  genre?: string[];
  // New quick-access fields populated from backend/enrichment
  malId?: string | number;
  tmdbId?: string | number;
  posterPath?: string; // backend local poster path or URL
  isAnimated?: boolean;
  origin?: string; // e.g., 'JP', 'US'
  releaseYear?: number;
  runtime?: number; // in minutes
  cast?: string[];
  is_identified?: boolean;
  season_count?: number;
  has_multiple_seasons?: boolean;
  seriesId?: string | number;
  seasonNumber?: number;
  isAiring?: boolean;
  airingStatus?: string;
  addedAt: string;
  files?: Array<{ filename?: string; path?: string; size?: number; mtime?: number; index?: number }>;
  rawMetadata?: any;
}

export interface ScanStatus {
  // Backend may provide total/processed counts and current item; make them optional
  total?: number;
  processed?: number;
  titles_total?: number;
  titles_processed?: number;
  queued?: number;
  scanning: boolean;
  progress: number;
  currentFile?: string;
  // alternate names some backends use
  current?: string;
  totalFiles?: number;
  processedFiles?: number;
  status?: string;
  // enrichment status returned from backend (optional)
  enrichment?: EnrichmentState;
  phase?: string | null;
  last_item?: { title?: string | null; path?: string | null; id?: number | string | null } | null;
  root_counts?: Record<string, any> | null;
  watch?: { path?: string | null; ts?: number | null } | null;
}

export interface EnrichmentState {
  running: boolean;
  current_id?: number | string | null;
  current_title?: string | null;
  current_step?: string | null;
  last_updated?: number | null;
  // DB ingestion counts
  total?: number;
  pending?: number;
  enriched?: number;
}

export interface Drive {
  path: string;
  label: string;
  totalSpace: number;
  freeSpace: number;
}

export interface MetadataConfig {
  moviesProvider: 'tmdb' | 'tvdb' | 'omdb';
  // Backend-supported: jikan | kitsu | tmdb | auto
  // Note: "auto" prefers Jikan when healthy and falls back to TMDB with strict matching.
  animeProvider: 'jikan' | 'kitsu' | 'tmdb' | 'auto';
  language: string;
  downloadImages: boolean;
  fetchCast: boolean;
}

export interface UserProfile {
  id: string;
  name: string;
  avatarColor: string; // Tailwind class like 'bg-indigo-500'
  avatarImage?: string; // data URL (optional)
  isKid: boolean;
  // Perfil de gestión: puede acceder a Ajustes/administración del servidor
  isManager?: boolean;
  // PIN corto y opcional (recomendado 4–6 dígitos)
  pin?: string;
  language: string;
}

export interface Notification {
  id: string;
  type: 'success' | 'error' | 'info';
  message: string;
  durationMs?: number;
  variant?: 'default' | 'welcome';
}

export interface TelemetryPhase {
  status?: string;
  started_at?: number;
  finished_at?: number;
  duration_ms?: number;
  total_ms?: number;
}

export interface TelemetryTask {
  task_id: string;
  task_kind?: string | null;
  status?: string;
  phases?: Record<string, TelemetryPhase>;
  started_at?: number | null;
  finished_at?: number | null;
  last_updated?: number | null;
  title?: string | null;
  media_item_id?: number | string | null;
  current_phase?: string | null;
  repeat_count?: number | null;
}

export interface TelemetryEvent {
  type: string;
  ts: number;
  task_id?: string;
  task_kind?: string;
  phase?: string;
  status?: string;
  duration_ms?: number;
  memory_mb?: number;
  level?: string;
  message?: string;
  logger?: string;
  provider?: string;
  service?: string;
  endpoint?: string;
  status_code?: number;
  error?: string;
  title?: string | null;
  media_item_id?: number | string | null;
}

export interface AdminEntityRow {
  id: number | string;
  title?: string | null;
  base_title?: string | null;
  canonical_path?: string | null;
  canonical_path_hash?: string | null;
  tmdb_id?: string | null;
  media_type?: string | null;
  status?: string | null;
  created_at?: string | null;
  path?: string | null;
}

export interface ConflictGroup {
  key: string;
  count: number;
  items: AdminEntityRow[];
  kind?: string;
}

export type AppStage = 'booting' | 'setup' | 'profile_select' | 'app';
export type ViewState = 'home' | 'movies' | 'tv' | 'list' | 'settings' | 'library';
