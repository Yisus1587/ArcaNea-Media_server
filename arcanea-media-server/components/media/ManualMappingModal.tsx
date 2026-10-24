import React, { useEffect, useMemo, useState } from 'react';
import { X, Search, Check, Loader2, Save } from 'lucide-react';
import { MediaItem } from '../../types';
import { Button } from '../ui/Button';
import { MediaService } from '../../services/api';
import { useI18n } from '../../i18n/i18n';

interface ManualMappingModalProps {
  item: MediaItem;
  onClose: () => void;
  onSaved?: () => void;
}

type SearchResult = {
  provider: 'tmdb' | 'jikan' | 'kitsu';
  tmdb_id?: string;
  mal_id?: string;
  kitsu_id?: string;
  media_type?: 'tv' | 'movie';
  title: string;
  year?: string;
  poster?: string | null;
};

type LocalSeason = {
  media_item_id: number;
  season_id?: number | null;
  season_number: number | null;
  title_en?: string | null;
  title_localized?: string | null;
  canonical_path?: string | null;
};

export const ManualMappingModal: React.FC<ManualMappingModalProps> = ({ item, onClose, onSaved }) => {
  const { lang, t } = useI18n();
  const languageOptions = [
    { value: 'es-MX', label: 'Español (MX)' },
    { value: 'es-ES', label: 'Español (ES)' },
    { value: 'es-AR', label: 'Español (AR)' },
    { value: 'pt-BR', label: 'Portugués (BR)' },
    { value: 'en-US', label: 'Inglés (US)' },
  ];
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  const defaultLang = useMemo(() => {
    const l = String(lang || '').toLowerCase();
    if (l.startsWith('es')) return 'es-MX';
    if (l.startsWith('pt')) return 'pt-BR';
    return 'en-US';
  }, [lang]);

  const initialProvider = useMemo<'tmdb' | 'jikan' | 'kitsu'>(() => {
    try {
      const p = String((item as any)?.rawMetadata?.provider || '').trim().toLowerCase();
      if (p === 'tmdb' || p === 'jikan' || p === 'kitsu') return p;
    } catch {
      // ignore
    }
    return item?.type === 'anime' ? 'kitsu' : 'tmdb';
  }, [item]);
  const [provider, setProvider] = useState<'tmdb' | 'jikan' | 'kitsu'>(initialProvider);
  const [query, setQuery] = useState(item.title || '');
  const [tmdbIdInput, setTmdbIdInput] = useState('');
  const [malIdInput, setMalIdInput] = useState('');
  const [kitsuIdInput, setKitsuIdInput] = useState('');
  const [language, setLanguage] = useState(defaultLang);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [selected, setSelected] = useState<SearchResult | null>(null);
  const [seasons, setSeasons] = useState<{ season_number: number; name?: string }[]>([]);
  const [seasonNumber, setSeasonNumber] = useState<number | null>(null);
  const [seasonTitle, setSeasonTitle] = useState<string>('');
  const [title, setTitle] = useState<string>('');
  const [overview, setOverview] = useState<string>('');
  const [genres, setGenres] = useState<string[]>([]);
  const [episodes, setEpisodes] = useState<{ episode_number: number; title: string; overview?: string; keep?: boolean }[]>([]);
  const [posters, setPosters] = useState<string[]>([]);
  const [backdrops, setBackdrops] = useState<string[]>([]);
  const [posterSelected, setPosterSelected] = useState<string | null>(null);
  const [backdropSelected, setBackdropSelected] = useState<string | null>(null);
  // Stable artwork pick for "bulk apply to seasons".
  // Prevents copying a season-specific poster/backdrop to every season.
  const [seriesPosterSelected, setSeriesPosterSelected] = useState<string | null>(null);
  const [seriesBackdropSelected, setSeriesBackdropSelected] = useState<string | null>(null);
  const [expandedImage, setExpandedImage] = useState<string | null>(null);
  const [posterLimit, setPosterLimit] = useState(12);
  const [backdropLimit, setBackdropLimit] = useState(6);
  const [busy, setBusy] = useState(false);
  const [seasonLoading, setSeasonLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [downloadAssets, setDownloadAssets] = useState(true);
  const [fetchEpisodesJikan, setFetchEpisodesJikan] = useState(false);
  const [advancedTmdbId, setAdvancedTmdbId] = useState('');
  const [advancedMediaType, setAdvancedMediaType] = useState<'tv' | 'movie'>('tv');
  const [advancedSeasonNumber, setAdvancedSeasonNumber] = useState('');
  const [seriesOverview, setSeriesOverview] = useState<string>('');
  const [localSeasons, setLocalSeasons] = useState<LocalSeason[]>([]);
  const [applyToSeasonIds, setApplyToSeasonIds] = useState<number[]>([]);
  const [localSeasonsLoading, setLocalSeasonsLoading] = useState(false);

  const localSeasonByItemId = useMemo(() => {
    const m = new Map<number, LocalSeason>();
    (localSeasons || []).forEach((s) => {
      const mid = Number((s as any)?.media_item_id);
      if (Number.isFinite(mid) && mid > 0) m.set(mid, s);
    });
    return m;
  }, [localSeasons]);

  const sortedLocalSeasons = useMemo(() => {
    const arr = [...(localSeasons || [])];
    arr.sort((a, b) => {
      const an = a?.season_number ?? null;
      const bn = b?.season_number ?? null;
      if (typeof an === 'number' && typeof bn === 'number' && an !== bn) return an - bn;
      if (typeof an === 'number' && typeof bn !== 'number') return -1;
      if (typeof an !== 'number' && typeof bn === 'number') return 1;
      return (a?.media_item_id || 0) - (b?.media_item_id || 0);
    });
    return arr;
  }, [localSeasons]);

  const displayEpisodeMap = useMemo(() => {
    const kept = episodes.filter(e => e.keep !== false);
    const ordered = kept.sort((a, b) => (a.episode_number || 0) - (b.episode_number || 0));
    const map = new Map<number, number>();
    ordered.forEach((e, idx) => {
      if (typeof e.episode_number === 'number') map.set(e.episode_number, idx + 1);
    });
    return map;
  }, [episodes]);

  useEffect(() => {
    setLanguage(defaultLang);
  }, [defaultLang]);

  useEffect(() => {
    setProvider(initialProvider);
  }, [initialProvider]);

  useEffect(() => {
    const itemId = Number(item?.id);
    if (!Number.isFinite(itemId) || itemId <= 0) {
      setLocalSeasons([]);
      setApplyToSeasonIds([]);
      return;
    }
    setApplyToSeasonIds([itemId]);
    if (item?.type !== 'series') {
      setLocalSeasons([]);
      return;
    }
    setLocalSeasonsLoading(true);
    MediaService.manualLocalSeasons(itemId)
      .then((res: any) => {
        const list = Array.isArray(res?.seasons) ? res.seasons : [];
        const normalized: LocalSeason[] = list
          .map((s: any) => ({
            media_item_id: Number(s?.media_item_id || 0),
            season_id: s?.season_id != null ? Number(s.season_id) : null,
            season_number: s?.season_number != null ? Number(s.season_number) : null,
            title_en: s?.title_en || null,
            title_localized: s?.title_localized || null,
            canonical_path: s?.canonical_path || null,
          }))
          .filter((s: LocalSeason) => Number.isFinite(s.media_item_id) && s.media_item_id > 0);
        setLocalSeasons(normalized);
        // Keep default selection as "only this item" to avoid accidental bulk writes.
        setApplyToSeasonIds([itemId]);
      })
      .catch((e: any) => {
        console.warn('manualLocalSeasons failed', e);
        setLocalSeasons([]);
        setApplyToSeasonIds([itemId]);
      })
      .finally(() => setLocalSeasonsLoading(false));
  }, [item?.id, item?.type]);

  useEffect(() => {
    if (provider !== 'tmdb') return;
    if (!advancedTmdbId && selected?.tmdb_id) {
      setAdvancedTmdbId(String(selected.tmdb_id));
    }
    if (selected?.media_type) {
      setAdvancedMediaType(selected.media_type === 'movie' ? 'movie' : 'tv');
    }
  }, [provider, selected, advancedTmdbId]);

  useEffect(() => {
    setResults([]);
    setSelected(null);
    setSeasons([]);
    setSeasonNumber(null);
    setEpisodes([]);
    setSeasonTitle('');
    setTmdbIdInput('');
    setMalIdInput('');
    setKitsuIdInput('');
  }, [provider]);

  const handleSearch = async () => {
    setBusy(true);
    setMessage(null);
    try {
      if (provider === 'jikan') {
        if (malIdInput.trim()) {
          const detail = await MediaService.manualJikanDetails(malIdInput.trim());
          const r: SearchResult = {
            provider: 'jikan',
            mal_id: malIdInput.trim(),
            media_type: (detail?.media_type === 'movie' ? 'movie' : 'tv') as 'movie' | 'tv',
            title: detail?.title || '',
            year: detail?.year || '',
            poster: detail?.poster || null,
          };
          setResults([r]);
          setSelected(r);
          loadDetails(r);
          return;
        }
        const res = await MediaService.manualSearchJikan(query);
        const list = Array.isArray(res?.results) ? res.results : [];
        const normalized = list.map((r: any) => ({
          provider: 'jikan' as const,
          mal_id: String(r?.mal_id ?? r?.id ?? '').trim(),
          media_type: (r?.media_type === 'movie' ? 'movie' : 'tv') as 'movie' | 'tv',
          title: r?.title || '',
          year: r?.year || '',
          poster: r?.poster || null,
        }));
        setResults(normalized);
      } else if (provider === 'kitsu') {
        if (kitsuIdInput.trim()) {
          const detail = await MediaService.manualKitsuDetails(kitsuIdInput.trim());
          const r: SearchResult = {
            provider: 'kitsu',
            kitsu_id: kitsuIdInput.trim(),
            media_type: (detail?.media_type === 'movie' ? 'movie' : 'tv') as 'movie' | 'tv',
            title: detail?.title || '',
            year: detail?.year || '',
            poster: detail?.poster || null,
          };
          setResults([r]);
          setSelected(r);
          loadDetails(r);
          return;
        }
        const res = await MediaService.manualSearchKitsu(query);
        const list = Array.isArray(res?.results) ? res.results : [];
        const normalized = list.map((r: any) => ({
          provider: 'kitsu' as const,
          kitsu_id: String(r?.kitsu_id ?? r?.id ?? '').trim(),
          media_type: (r?.media_type === 'movie' ? 'movie' : 'tv') as 'movie' | 'tv',
          title: r?.title || '',
          year: r?.year || '',
          poster: r?.poster || null,
        }));
        setResults(normalized);
      } else {
        if (tmdbIdInput.trim()) {
          const detail = await MediaService.manualTmdbDetails(tmdbIdInput.trim(), 'tv', language);
          const r: SearchResult = {
            provider: 'tmdb',
            tmdb_id: tmdbIdInput.trim(),
            media_type: 'tv',
            title: detail?.title || '',
            year: detail?.year || '',
            poster: detail?.poster || null,
          };
          setResults([r]);
          setSelected(r);
          loadDetails(r);
          return;
        }
        const res = await MediaService.manualSearchTmdb(query, language);
        const list = Array.isArray(res?.results) ? res.results : [];
        const normalized = list.map((r: any) => ({
          provider: 'tmdb' as const,
          tmdb_id: String(r?.tmdb_id ?? r?.id ?? '').trim(),
          media_type: (r?.media_type === 'movie' ? 'movie' : 'tv') as 'movie' | 'tv',
          title: r?.title || '',
          year: r?.year || '',
          poster: r?.poster || null,
        }));
        setResults(normalized);
      }
    } catch (e: any) {
      setMessage(String(e?.message || e));
      setResults([]);
    } finally {
      setBusy(false);
    }
  };

  const loadDetails = async (r: SearchResult) => {
    setBusy(true);
    setMessage(null);
    try {
      if (r.provider === 'jikan') {
        const detail = await MediaService.manualJikanDetails(String(r.mal_id || ''));
        setTitle(detail?.title || r.title || '');
        setOverview(detail?.overview || '');
        setSeriesOverview(detail?.overview || '');
        setGenres(Array.isArray(detail?.genres) ? detail.genres : []);
        const posters = Array.isArray(detail?.posters) ? detail.posters : [];
        const posterPick = posters[0] || detail?.poster || r.poster || null;
        setPosters(posters.length ? posters : (detail?.poster ? [detail.poster] : []));
        setBackdrops([]);
        setPosterSelected(posterPick);
        setBackdropSelected(null);
        setSeriesPosterSelected(posterPick);
        setSeriesBackdropSelected(null);
        setPosterLimit(12);
        setBackdropLimit(6);
        setSeasons([]);
        setSeasonNumber(null);
        setEpisodes([]);
      } else if (r.provider === 'kitsu') {
        const detail = await MediaService.manualKitsuDetails(String(r.kitsu_id || ''));
        setTitle(detail?.title || r.title || '');
        setOverview(detail?.overview || '');
        setSeriesOverview(detail?.overview || '');
        setGenres(Array.isArray(detail?.genres) ? detail.genres : []);
        const posters = Array.isArray(detail?.posters) ? detail.posters : [];
        const backdrops = Array.isArray(detail?.backdrops) ? detail.backdrops : [];
        const posterPick = posters[0] || detail?.poster || r.poster || null;
        const backdropPick = backdrops[0] || detail?.backdrop || null;
        setPosters(posters.length ? posters : (detail?.poster ? [detail.poster] : []));
        setBackdrops(backdrops.length ? backdrops : (detail?.backdrop ? [detail.backdrop] : []));
        setPosterSelected(posterPick);
        setBackdropSelected(backdropPick);
        setSeriesPosterSelected(posterPick);
        setSeriesBackdropSelected(backdropPick);
        setPosterLimit(12);
        setBackdropLimit(6);
        setSeasons([]);
        setSeasonNumber(null);
        setEpisodes([]);
      } else {
        const detail = await MediaService.manualTmdbDetails(String(r.tmdb_id || ''), r.media_type || 'tv', language);
        setTitle(detail?.title || r.title || '');
        setOverview(detail?.overview || '');
        setSeriesOverview(detail?.overview || '');
        setGenres(Array.isArray(detail?.genres) ? detail.genres : []);
        const posters = Array.isArray(detail?.posters) ? detail.posters : [];
        const backdrops = Array.isArray(detail?.backdrops) ? detail.backdrops : [];
        const posterPick = posters[0] || detail?.poster || r.poster || null;
        const backdropPick = backdrops[0] || detail?.backdrop || null;
        setPosters(posters.length ? posters : (detail?.poster ? [detail.poster] : []));
        setBackdrops(backdrops.length ? backdrops : (detail?.backdrop ? [detail.backdrop] : []));
        setPosterSelected(posterPick);
        setBackdropSelected(backdropPick);
        setSeriesPosterSelected(posterPick);
        setSeriesBackdropSelected(backdropPick);
        setPosterLimit(12);
        setBackdropLimit(6);
        if (r.media_type === 'tv') {
          const s = await MediaService.manualTmdbSeasons(String(r.tmdb_id || ''), language);
          setSeasons(Array.isArray(s?.seasons) ? s.seasons : []);
        } else {
          setSeasons([]);
          setSeasonNumber(null);
          setEpisodes([]);
        }
      }
    } catch (e: any) {
      setMessage(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const loadSeason = async (sn: number) => {
    if (!selected || selected.provider !== 'tmdb') return;
    setSeasonLoading(true);
    setMessage(null);
    try {
      const d = await MediaService.manualTmdbSeasonDetails(String(selected.tmdb_id || ''), sn, language);
      const eps = Array.isArray(d?.episodes) ? d.episodes : [];
      setSeasonTitle(d?.title || '');
      setOverview(d?.overview || '');
      setEpisodes(
        eps.map((e: any) => ({
          episode_number: Number(e?.episode_number || 0),
          title: e?.title || '',
          overview: e?.overview || '',
          keep: true,
        }))
      );
      const p = Array.isArray(d?.posters) ? d.posters : [];
      const b = Array.isArray(d?.backdrops) ? d.backdrops : [];
      const mergedPosters = Array.from(new Set([...(posters || []), ...p]));
      const mergedBackdrops = Array.from(new Set([...(backdrops || []), ...b]));
      setPosters(mergedPosters);
      setBackdrops(mergedBackdrops);
      if (!posterSelected && mergedPosters.length) setPosterSelected(mergedPosters[0]);
      if (!backdropSelected && mergedBackdrops.length) setBackdropSelected(mergedBackdrops[0]);
      setPosterLimit(12);
      setBackdropLimit(6);
    } catch (e: any) {
      setMessage(String(e?.message || e));
    } finally {
      setSeasonLoading(false);
    }
  };

  const handleAdvancedLoad = async () => {
    if (provider !== 'tmdb') return;
    const tid = String(advancedTmdbId || '').trim();
    if (!tid) {
      setMessage('TMDB ID requerido');
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const r: SearchResult = {
        provider: 'tmdb',
        tmdb_id: tid,
        media_type: advancedMediaType,
        title: tid,
      };
      setSelected(r);
      setResults([r]);

      const detail = await MediaService.manualTmdbDetails(tid, advancedMediaType, language);
      setTitle(detail?.title || r.title || '');
      const baseOverview = detail?.overview || '';
      setOverview(baseOverview);
      setSeriesOverview(baseOverview);
      setGenres(Array.isArray(detail?.genres) ? detail.genres : []);
      const posters = Array.isArray(detail?.posters) ? detail.posters : [];
      const backdrops = Array.isArray(detail?.backdrops) ? detail.backdrops : [];
      const posterPick = posters[0] || detail?.poster || null;
      const backdropPick = backdrops[0] || detail?.backdrop || null;
      setPosters(posters.length ? posters : (detail?.poster ? [detail.poster] : []));
      setBackdrops(backdrops.length ? backdrops : (detail?.backdrop ? [detail.backdrop] : []));
      setPosterSelected(posterPick);
      setBackdropSelected(backdropPick);
      setSeriesPosterSelected(posterPick);
      setSeriesBackdropSelected(backdropPick);
      setPosterLimit(12);
      setBackdropLimit(6);

      if (advancedMediaType === 'tv') {
        const snRaw = String(advancedSeasonNumber || '').trim();
        const sn = snRaw ? Number(snRaw) : null;
        if (sn && Number.isFinite(sn)) {
          const seasonDetail = await MediaService.manualTmdbSeasonDetails(tid, sn, language);
          const eps = Array.isArray(seasonDetail?.episodes) ? seasonDetail.episodes : [];
          setSeasonTitle(seasonDetail?.title || '');
          setOverview(seasonDetail?.overview || baseOverview);
          setEpisodes(
            eps.map((e: any) => ({
              episode_number: Number(e?.episode_number || 0),
              title: e?.title || '',
              overview: e?.overview || '',
              keep: true,
            }))
          );
          setSeasonNumber(sn);
          setSeasons([{ season_number: sn, name: seasonDetail?.title || '' }]);
        } else {
          setSeasonNumber(null);
          setSeasons([]);
          setEpisodes([]);
          setSeasonTitle('');
        }
      } else {
        setSeasonNumber(null);
        setSeasons([]);
        setEpisodes([]);
        setSeasonTitle('');
      }
    } catch (e: any) {
      setMessage(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const handleApply = async () => {
    if (!selected) return;
    setBusy(true);
    setMessage(null);
    try {
      const itemId = Number(item.id);
      const targets: number[] = Array.from(new Set(applyToSeasonIds)).filter((n): n is number => (
        typeof n === 'number' && Number.isFinite(n) && n > 0
      ));
      const target0: number = targets[0] ?? (Number.isFinite(itemId) ? itemId : Number(item.id));
      const isBulkSeriesApply = provider === 'tmdb' && selected.provider === 'tmdb' && selected.media_type === 'tv' && targets.length > 1;

      if (isBulkSeriesApply) {
        const basePayload = {
          provider,
          tmdb_id: selected.tmdb_id,
          mal_id: null,
          kitsu_id: null,
          media_type: selected.media_type,
          // season_number will be set per item (from local DB) to preserve the correct season mapping.
          season_number: null as number | null,
          language,
          // Bulk apply is identity-first; avoid overwriting per-season title/overview.
          title: null,
          overview: null,
          genres,
          // Use stable series artwork for bulk apply (not a season-specific selection).
          poster_url: seriesPosterSelected,
          backdrop_url: seriesBackdropSelected,
          // Bulk apply is for identity + stable artwork; don't overwrite season titles or renumber episodes.
          season_title: null,
          download_assets: true,
          fetch_episodes: false,
          episode_overrides: [],
        };
        for (let i = 0; i < targets.length; i++) {
          const targetId = targets[i];
          const localSeasonNumber = localSeasonByItemId.get(targetId)?.season_number ?? null;
          const payload = { ...basePayload, season_number: localSeasonNumber };
          setMessage(`${t('saving')} (${i + 1}/${targets.length})`);
          const res = await MediaService.manualApplyMapping(targetId, payload);
          if (!res?.ok) throw new Error(res?.error || res?.detail || 'save_failed');
        }
      } else {
        const payload = {
          provider,
          tmdb_id: provider === 'tmdb' ? selected.tmdb_id : null,
          mal_id: provider === 'jikan' ? selected.mal_id : null,
          kitsu_id: provider === 'kitsu' ? selected.kitsu_id : null,
          media_type: provider === 'tmdb' ? selected.media_type : null,
          season_number: seasonNumber,
          language,
          title: title || seasonTitle || selected.title,
          overview,
          genres,
          poster_url: posterSelected,
          backdrop_url: backdropSelected,
          season_title: seasonTitle || title || '',
          download_assets: true,
          fetch_episodes: fetchEpisodesJikan,
          episode_overrides: provider === 'tmdb' ? (() => {
            const kept = episodes.filter(e => e.keep !== false);
            const ordered = kept.sort((a, b) => (a.episode_number || 0) - (b.episode_number || 0));
            return ordered.map((e, idx) => ({
              episode_number: idx + 1,
              original_episode_number: e.episode_number,
              title: e.title,
              overview: e.overview,
            }));
          })() : [],
        };
        const res = await MediaService.manualApplyMapping(target0, payload);
        if (!res?.ok) throw new Error(res?.error || res?.detail || 'save_failed');
      }
      
      setMessage(t('saved_successfully'));
      
      // Auto-clear message after 3 seconds
      setTimeout(() => setMessage(null), 3000);
      
      if (onSaved) onSaved();
    } catch (e: any) {
      setMessage(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  const content = (
    <div className="fixed inset-0 z-[260] flex items-center justify-center p-4 sm:p-6 animate-fade-in">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-md" onClick={onClose} />
      <div className="relative w-full max-w-5xl bg-[#18181b] rounded-2xl overflow-hidden shadow-2xl border border-white/10 flex flex-col max-h-[90vh]">
        <button
          onClick={onClose}
          className="absolute top-4 right-4 z-20 p-2 rounded-full bg-black/40 hover:bg-black/60 text-white backdrop-blur-sm border border-white/10 transition-all"
        >
          <X size={22} />
        </button>

        <div className="px-6 pt-6 pb-4 border-b border-white/10 flex items-center justify-between">
          <div>
            <h3 className="text-xl text-white font-semibold">{t('manual_mapping_title')}</h3>
            <p className="text-sm text-slate-400">{t('manual_mapping_subtitle')}</p>
          </div>
          {message && (
            <div className={`px-4 py-2 rounded-xl text-sm font-black animate-in zoom-in-95 duration-300 ${message.toLowerCase().includes('failed') || message.toLowerCase().includes('error') ? 'bg-red-500/20 text-red-400 border border-red-500/30' : 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 shadow-[0_0_20px_rgba(16,185,129,0.2)]'}`}>
              {message}
            </div>
          )}
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="md:col-span-2 space-y-3">
              <div className="flex items-center gap-2 text-sm text-slate-300">
                <span>{t('manual_mapping_provider')}:</span>
                <select
                  className="bg-black/30 border border-white/10 rounded-lg px-2 py-1 text-white"
                  value={provider}
                  onChange={(e) => setProvider(e.target.value as 'tmdb' | 'jikan' | 'kitsu')}
                >
                  <option value="tmdb">{t('manual_mapping_provider_tmdb')}</option>
                  <option value="kitsu">{t('manual_mapping_provider_kitsu')}</option>
                  <option value="jikan">{t('manual_mapping_provider_jikan')}</option>
                </select>
              </div>
              {provider === 'tmdb' ? (
                <div className="mt-2 p-3 rounded-xl bg-white/5 border border-white/10">
                  <div className="text-[11px] uppercase tracking-wider text-slate-400 mb-2">
                    {t('manual_mapping_advanced')}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      className="w-40 bg-black/30 border border-white/10 rounded-lg px-2 py-1 text-white text-xs"
                      placeholder={t('manual_mapping_tmdb_id_existing')}
                      value={advancedTmdbId}
                      onChange={(e) => setAdvancedTmdbId(e.target.value)}
                    />
                    <select
                      className="bg-black/30 border border-white/10 rounded-lg px-2 py-1 text-white text-xs"
                      value={advancedMediaType}
                      onChange={(e) => setAdvancedMediaType(e.target.value as 'tv' | 'movie')}
                    >
                      <option value="tv">TV</option>
                      <option value="movie">Movie</option>
                    </select>
                    <input
                      className="w-24 bg-black/30 border border-white/10 rounded-lg px-2 py-1 text-white text-xs"
                      placeholder={t('manual_mapping_season_number')}
                      value={advancedSeasonNumber}
                      onChange={(e) => setAdvancedSeasonNumber(e.target.value)}
                    />
                    <Button onClick={handleAdvancedLoad} disabled={busy}>
                      {t('manual_mapping_load_by_id')}
                    </Button>
                  </div>
                  <div className="mt-2 text-[11px] text-slate-500">
                    {`TMDB /tv/{id}/season/{#}`}
                  </div>
                </div>
              ) : null}
              <div className="flex gap-2">
                <input
                  className="flex-1 bg-black/30 border border-white/10 rounded-lg px-3 py-2 text-white"
                  placeholder={provider === 'jikan'
                    ? t('manual_mapping_search_placeholder_jikan')
                    : provider === 'kitsu'
                      ? t('manual_mapping_search_placeholder_kitsu')
                      : t('manual_mapping_search_placeholder_tmdb')}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      if (!busy) void handleSearch();
                    }
                  }}
                />
                {provider === 'tmdb' ? (
                  <input
                    className="w-24 bg-black/30 border border-white/10 rounded-lg px-2 py-1 text-white text-xs"
                    placeholder="TMDB ID"
                    value={tmdbIdInput}
                    onChange={(e) => setTmdbIdInput(e.target.value)}
                  />
                ) : (
                  <input
                    className="w-24 bg-black/30 border border-white/10 rounded-lg px-2 py-1 text-white text-xs"
                    placeholder={provider === 'kitsu' ? 'KITSU ID' : 'MAL ID'}
                    value={provider === 'kitsu' ? kitsuIdInput : malIdInput}
                    onChange={(e) => (provider === 'kitsu' ? setKitsuIdInput(e.target.value) : setMalIdInput(e.target.value))}
                  />
                )}
                <Button icon={<Search size={16} />} onClick={handleSearch} disabled={busy}>
                  {t('manual_mapping_search')}
                </Button>
              </div>
              <div className="flex items-center gap-2 text-sm text-slate-300">
                <span>{t('language')}:</span>
                <select
                  className="bg-black/30 border border-white/10 rounded-lg px-2 py-1 text-white"
                  value={language}
                  onChange={(e) => setLanguage(e.target.value)}
                  disabled={provider === 'jikan' || provider === 'kitsu'}
                >
                  {languageOptions.map((opt) => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
                <label className="ml-3 inline-flex items-center gap-2 text-xs text-slate-300 cursor-pointer hover:text-white transition-colors">
                  <input
                    type="checkbox"
                    className="accent-indigo-500 w-4 h-4 rounded border-white/10 bg-black/30"
                    checked={true}
                    disabled
                    onChange={() => setDownloadAssets(true)}
                  />
                  {t('download_assets_local')}
                </label>
                {provider === 'jikan' && (
                  <label className="ml-3 inline-flex items-center gap-2 text-xs text-slate-300 cursor-pointer hover:text-white transition-colors animate-in fade-in slide-in-from-left-2">
                    <input
                      type="checkbox"
                      className="accent-indigo-500 w-4 h-4 rounded border-white/10 bg-black/30"
                      checked={fetchEpisodesJikan}
                      onChange={(e) => setFetchEpisodesJikan(e.target.checked)}
                    />
                    {t('fetch_episodes_jikan')}
                  </label>
                )}
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {(selected ? results.filter(r => {
                  if (r.provider !== selected.provider) return false;
                  const idOk =
                    r.provider === 'jikan'
                      ? r.mal_id === selected.mal_id
                      : r.provider === 'kitsu'
                        ? r.kitsu_id === selected.kitsu_id
                        : r.tmdb_id === selected.tmdb_id;
                  return idOk && r.media_type === selected.media_type;
                }) : results).map((r) => (
                  <button
                    key={`${r.provider}-${r.media_type}-${r.tmdb_id || r.mal_id || r.kitsu_id}`}
                    onClick={() => {
                      setSelected(r);
                      setSeasonNumber(null);
                      setEpisodes([]);
                      loadDetails(r);
                    }}
                    className={`w-full flex items-center gap-3 p-3 rounded-2xl border transition ${
                      (selected?.provider === r.provider && (
                        r.provider === 'jikan'
                          ? selected?.mal_id === r.mal_id
                          : r.provider === 'kitsu'
                            ? selected?.kitsu_id === r.kitsu_id
                            : selected?.tmdb_id === r.tmdb_id
                      ))
                        ? 'border-indigo-500/50 bg-white/10 shadow-[0_0_20px_rgba(79,70,229,0.3)]'
                        : 'border-white/10 bg-white/5'
                    }`}
                  >
                    <div className="w-12 h-16 rounded-lg overflow-hidden border border-white/10 bg-white/5 shrink-0">
                      <img src={r.poster || ''} className="w-full h-full object-cover" />
                    </div>
                    <div className="text-left min-w-0">
                      <div className="text-white font-semibold truncate">{r.title}</div>
                      <div className="text-xs text-slate-400">{(r.media_type || 'tv').toUpperCase()} · {r.year || '—'}</div>
                    </div>
                  </button>
                ))}
              </div>
              {selected ? (
                <div className="flex justify-end">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setSelected(null)}
                    disabled={busy}
                  >
                    {t('change_search')}
                  </Button>
                </div>
              ) : null}
            </div>

            <div className="space-y-3">
              <div className="text-sm text-slate-400">{t('seasons')}</div>
              <select
                className="w-full bg-black/30 border border-white/10 rounded-lg px-3 py-2 text-white"
                value={seasonNumber ?? ''}
                onChange={(e) => {
                  const sn = Number(e.target.value);
                  setSeasonNumber(Number.isFinite(sn) ? sn : null);
                  if (Number.isFinite(sn)) {
                    const match = sortedLocalSeasons.find((s) => s?.season_number === sn);
                    if (match && Number.isFinite(Number(match.media_item_id)) && Number(match.media_item_id) > 0) {
                      setApplyToSeasonIds([Number(match.media_item_id)]);
                    }
                    loadSeason(sn);
                  }
                }}
                disabled={!selected || selected.provider !== 'tmdb' || selected.media_type !== 'tv'}
              >
                <option value="">{t('select_season')}</option>
                {seasons.map((s) => (
                  <option key={s.season_number} value={s.season_number}>{`S${s.season_number} ${s.name || ''}`}</option>
                ))}
              </select>
              {provider === 'jikan' ? (
                <div className="text-xs text-slate-500">{t('not_available_tmdb_only')}</div>
              ) : null}
              {seasonLoading ? (
                <div className="flex items-center gap-2 text-xs text-slate-400">
                  <Loader2 size={14} className="animate-spin" />
                  {t('loading_episodes_images')}
                </div>
              ) : null}

              {provider === 'tmdb' && selected?.provider === 'tmdb' && selected?.media_type === 'tv' && (sortedLocalSeasons.length > 1 || localSeasonsLoading) ? (
                <div className="mt-2 p-3 rounded-xl bg-white/5 border border-white/10">
                  <div className="text-[11px] uppercase tracking-wider text-slate-400 mb-2">
                    {t('manual_mapping_apply_to_local_seasons')}
                  </div>
                  <div className="text-xs text-slate-500 mb-2">
                    {t('manual_mapping_apply_to_local_seasons_hint')}
                  </div>
                  {localSeasonsLoading ? (
                    <div className="flex items-center gap-2 text-xs text-slate-400">
                      <Loader2 size={14} className="animate-spin" />
                      {t('loading')}
                    </div>
                  ) : (
                    <div className="space-y-1 max-h-40 overflow-y-auto pr-1">
                      {sortedLocalSeasons.map((s) => {
                        const mid = Number(s.media_item_id);
                        const checked = applyToSeasonIds.includes(mid);
                        const sn = s.season_number;
                        const label = typeof sn === 'number' ? `S${sn}` : `ID ${mid}`;
                        return (
                          <label key={mid} className="flex items-center gap-2 text-xs text-slate-300">
                            <input
                              type="checkbox"
                              className="accent-indigo-500 w-4 h-4 rounded border-white/10 bg-black/30"
                              checked={checked}
                              disabled={busy}
                              onChange={() => {
                                setApplyToSeasonIds((prev) => {
                                  const has = prev.includes(mid);
                                  const next = has ? prev.filter((x) => x !== mid) : [...prev, mid];
                                  return next.length ? next : prev; // keep at least one selected
                                });
                              }}
                            />
                            <span className="text-slate-400">{label}</span>
                            <span className="truncate">{s.title_localized || s.title_en || ''}</span>
                          </label>
                        );
                      })}
                    </div>
                  )}
                  <div className="mt-2 flex justify-end gap-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => {
                        const id = Number(item?.id);
                        if (Number.isFinite(id) && id > 0) setApplyToSeasonIds([id]);
                      }}
                    >
                      {t('manual_mapping_select_only_this_season')}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy || !sortedLocalSeasons.length}
                      onClick={() => setApplyToSeasonIds(sortedLocalSeasons.map((s) => Number(s.media_item_id)).filter((n) => Number.isFinite(n) && n > 0))}
                    >
                      {t('manual_mapping_select_all_seasons')}
                    </Button>
                  </div>
                  {applyToSeasonIds.length > 1 ? (
                    <div className="mt-2 text-xs text-slate-500">
                      {t('manual_mapping_bulk_apply_note')}
                    </div>
                  ) : null}
                </div>
              ) : null}

              <div className="text-sm text-slate-400">{t('title')}</div>
              <input
                className="w-full bg-black/30 border border-white/10 rounded-lg px-3 py-2 text-white"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />

              <div className="text-sm text-slate-400">{t('synopsis')}</div>
              <textarea
                className="w-full bg-black/30 border border-white/10 rounded-lg px-3 py-2 text-white min-h-[120px]"
                value={overview}
                onChange={(e) => setOverview(e.target.value)}
              />
            </div>
          </div>

          {episodes.length > 0 ? (
            <div className="space-y-2">
              <div className="text-sm text-slate-400">{t('episodes')}</div>
              {seasonNumber != null ? (
                <div className="flex items-center justify-between">
                  <div className="text-xs text-slate-500">{t('episode_remove_hint')}</div>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setEpisodes([])}
                    disabled={busy}
                  >
                    {t('clear_episodes')}
                  </Button>
                </div>
              ) : null}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {episodes.filter(ep => ep.keep !== false).map((ep) => (
                  <div key={ep.episode_number} className="p-3 rounded-xl border border-white/10 bg-white/5">
                    <div className="text-xs text-slate-400">
                      E{displayEpisodeMap.get(ep.episode_number) ?? ep.episode_number}
                    </div>
                    <label className="flex items-center gap-2 text-xs text-slate-400 mt-2">
                      <input
                        type="checkbox"
                        checked={ep.keep !== false}
                        onChange={(e) => {
                          const keep = e.target.checked;
                          setEpisodes(prev => prev.map(p => p.episode_number === ep.episode_number ? { ...p, keep } : p));
                        }}
                      />
                      {t('include_in_season')}
                    </label>
                    <input
                      className="w-full bg-black/30 border border-white/10 rounded-lg px-2 py-1 text-white text-sm"
                      value={ep.title}
                      onChange={(e) => {
                        setEpisodes(prev => prev.map(p => p.episode_number === ep.episode_number ? { ...p, title: e.target.value } : p));
                      }}
                    />
                    <div className="flex justify-end mt-2">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setEpisodes(prev => prev.filter(p => p.episode_number !== ep.episode_number))}
                      >
                        {t('remove')}
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <div className="text-sm text-slate-400 mb-2">{t('posters')}</div>
              <div className="grid grid-cols-4 gap-2">
                {posters.slice(0, posterLimit).map((p) => (
                  <button key={p} onClick={() => setPosterSelected(p)} className="relative rounded-lg border border-white/10 bg-white/5">
                    <img src={p} className="w-full h-28 object-cover rounded-lg" />
                    {posterSelected === p ? (
                      <div className="absolute inset-0 bg-emerald-400/20 rounded-lg border border-emerald-400/60 flex items-center justify-center">
                        <Check size={16} className="text-emerald-200" />
                      </div>
                    ) : null}
                    <div
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        setExpandedImage(p);
                      }}
                      className="absolute top-2 right-2 text-[10px] px-2 py-1 rounded bg-black/60 text-white"
                    >
                      {t('view')}
                    </div>
                  </button>
                ))}
              </div>
              {posters.length > posterLimit ? (
                <div className="mt-2 flex justify-end">
                  <Button size="sm" variant="ghost" onClick={() => setPosterLimit((p) => p + 12)}>
                    {t('show_more')}
                  </Button>
                </div>
              ) : null}
            </div>
            <div>
              <div className="text-sm text-slate-400 mb-2">{t('backdrops')}</div>
              <div className="grid grid-cols-2 gap-2">
                {backdrops.slice(0, backdropLimit).map((b) => (
                  <button key={b} onClick={() => setBackdropSelected(b)} className="relative rounded-lg border border-white/10 bg-white/5">
                    <img src={b} className="w-full h-24 object-cover rounded-lg" />
                    {backdropSelected === b ? (
                      <div className="absolute inset-0 bg-emerald-400/20 rounded-lg border border-emerald-400/60 flex items-center justify-center">
                        <Check size={16} className="text-emerald-200" />
                      </div>
                    ) : null}
                    <div
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        setExpandedImage(b);
                      }}
                      className="absolute top-2 right-2 text-[10px] px-2 py-1 rounded bg-black/60 text-white"
                    >
                      {t('view')}
                    </div>
                  </button>
                ))}
              </div>
              {backdrops.length > backdropLimit ? (
                <div className="mt-2 flex justify-end">
                  <Button size="sm" variant="ghost" onClick={() => setBackdropLimit((b) => b + 6)}>
                    {t('show_more')}
                  </Button>
                </div>
              ) : null}
            </div>
          </div>

          {message ? (
            <div className="text-sm text-emerald-300">{message}</div>
          ) : null}
        </div>

        <div className="p-6 border-t border-white/10 flex justify-end gap-3 bg-slate-900/50">
          <Button variant="ghost" onClick={onClose} disabled={busy}>{t('cancel')}</Button>
          <Button onClick={handleApply} disabled={busy || !selected} icon={busy ? <Loader2 className="animate-spin" size={18} /> : <Save size={18} />}>
            {busy ? t('saving') : t('apply_mapping')}
          </Button>
        </div>
      </div>

      {expandedImage ? (
        <div className="fixed inset-0 z-[280] flex items-center justify-center p-6">
          <div className="absolute inset-0 bg-black/80" onClick={() => setExpandedImage(null)} />
          <div className="relative max-w-4xl w-full">
            <img src={expandedImage} className="w-full max-h-[80vh] object-contain rounded-xl border border-white/10" />
            <button
              onClick={() => setExpandedImage(null)}
              className="absolute top-3 right-3 bg-black/70 text-white rounded-full p-2"
            >
              <X size={18} />
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );

  return content;
};
