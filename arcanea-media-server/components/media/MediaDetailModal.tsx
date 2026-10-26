import React, { useEffect, useState } from 'react';
import { X, Play, Plus, ThumbsUp, Calendar, Clock, RefreshCw, Wrench, ImageOff, Check } from 'lucide-react';
import { MediaItem } from '../../types';
import { Button } from '../ui/Button';
import { MediaService, resolveAssetUrl } from '../../services/api';
import { useI18n } from '../../i18n/i18n';
import { ManualMappingModal } from './ManualMappingModal';
import { InlineLoader } from '../ui/InlineLoader';

interface MediaDetailModalProps {
  item: MediaItem;
  onClose: () => void;
  onPlay: (item: MediaItem) => void;
  isAdmin?: boolean;
  onManualMappingSaved?: (itemId: string) => void;
  profileId?: string;
  onListChanged?: () => void;
}

export const MediaDetailModal: React.FC<MediaDetailModalProps> = ({ item, onClose, onPlay, isAdmin, onManualMappingSaved, profileId, onListChanged }) => {
  const { lang, t } = useI18n();
  // Prevent scrolling on body when modal is open
  React.useEffect(() => {
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = 'unset'; };
  }, []);
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const [detail, setDetail] = useState<MediaItem | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshingMeta, setRefreshingMeta] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState<string | null>(null);
  const [selectedSeason, setSelectedSeason] = useState<number | null>(null);
  const [episodeRanges, setEpisodeRanges] = useState<Record<string, number>>({});
  const [dbEpisodeSeasons, setDbEpisodeSeasons] = useState<any[] | null>(null);
  const [dbEpisodeLoading, setDbEpisodeLoading] = useState(false);
  const [showManualMapping, setShowManualMapping] = useState(false);
  const [inUserList, setInUserList] = useState(false);
  const [listBusy, setListBusy] = useState(false);
  const fallbackPoster = '/images/arcanea-poster.svg';
  const fallbackBackdrop = '/images/arcanea-backdrop.svg';

  const _hasEpisodeHint = (p: string): boolean => {
    try {
      const s = String(p || '');
      if (!s) return false;
      if (/\bS\d{1,2}E\d{1,3}\b/i.test(s)) return true;
      if (/\b(?:temporada|season|temp(?:\.|orada)?)\s*\d{1,2}\b/i.test(s)) return true;
      if (/\bs\d{1,2}\b/i.test(s)) return true;
    } catch (e) {
      return false;
    }
    return false;
  };

  const looksLikeSeriesFromFiles = (files: any[]): boolean => {
    try {
      if (!Array.isArray(files) || files.length < 2) return false;
      for (const f of files) {
        const p = String(f?.path || f?.filename || '');
        if (_hasEpisodeHint(p)) return true;
      }
      // Fallback: multiple files usually implies episodic content.
      return true;
    } catch (e) {
      return false;
    }
  };

  useEffect(() => {
    let mounted = true;
    const load = async () => {
      setDetail(item);
      setLoading(true);
      try {
        const id = Number(item.id);
        const d = await MediaService.getById(id);
        if (mounted) setDetail(d);
      } catch (e) {
        console.warn('Error, en cargar', e);
        if (mounted) setDetail(item);
      } finally {
        if (mounted) setLoading(false);
      }
    };
    load();
    return () => { mounted = false; };
  }, [item]);

  useEffect(() => {
    let mounted = true;
    const run = async () => {
      const filesForDetect = ((detail && (detail as any).files) || (item as any).files || []) as any[];
      const seriesLike =
        (((detail && (detail as any).type) || item.type) === 'series') ||
        (((detail && (detail as any).type) || item.type) === 'anime') ||
        looksLikeSeriesFromFiles(filesForDetect);
      if (!seriesLike) {
        setDbEpisodeSeasons(null);
        return;
      }
      setDbEpisodeLoading(true);
      try {
        const id = Number(item.id);
        const res = await MediaService.getEpisodeSeasons(id, { includeRelated: true });
        const seasons = Array.isArray(res?.seasons) ? res.seasons : [];
        if (mounted) setDbEpisodeSeasons(seasons);
        // If there are multiple seasons, default the selector to the first season.
        if (mounted && seasons.length >= 2 && selectedSeason == null) {
          const sn = Number(seasons[0]?.season_number ?? seasons[0]?.seasonNumber ?? 1);
          if (Number.isFinite(sn)) setSelectedSeason(sn);
        }
      } catch (e) {
        if (mounted) setDbEpisodeSeasons(null);
      } finally {
        if (mounted) setDbEpisodeLoading(false);
      }
    };
    run();
    return () => { mounted = false; };
  }, [item.id, item.type, detail]);

  useEffect(() => {
    let mounted = true;
    const run = async () => {
      if (!profileId) return;
      try {
        const res = await MediaService.getUserListItem(Number(item.id), profileId);
        if (mounted) setInUserList(!!res?.in_list);
      } catch {
        if (mounted) setInUserList(false);
      }
    };
    run();
    return () => { mounted = false; };
  }, [item.id, profileId]);

  const handleRefreshMetadata = async () => {
    setRefreshingMeta(true);
    setRefreshMsg(null);
    try {
      const id = Number(item.id);
      const r = await MediaService.enrichOne(id);
      if (!r.ok) {
        setRefreshMsg(r.detail || t('refresh_metadata_error'));
        return;
      }
      const d = await MediaService.getById(id);
      setDetail(d);
      if (onManualMappingSaved) onManualMappingSaved(String(id));
      setRefreshMsg(t('updated_metadata'));
    } catch (e) {
      setRefreshMsg(String(e || t('refresh_metadata_error')));
    } finally {
      setRefreshingMeta(false);
    }
  };

  const meta: any = (detail && (detail as any).rawMetadata) || (item as any).rawMetadata || null;
  const metaRaw: any = meta && meta.raw ? meta.raw : meta;

  const filesForDetect = ((detail && (detail as any).files) || (item as any).files || []) as any[];
  const isSeriesLike =
    (((detail && (detail as any).type) || item.type) === 'series') ||
    (((detail && (detail as any).type) || item.type) === 'anime') ||
    looksLikeSeriesFromFiles(filesForDetect);

  const detectSeasonFromPath = (p: string): number | null => {
    try {
      const parts = String(p || '').split(/[\\/]/).filter(Boolean);
      for (const part of parts.slice().reverse()) {
        const seg = String(part || '').trim();
        if (!seg) continue;
        // Folder-like patterns: "Temporada 2", "Season 2", "S2", "S02"
        let m = seg.match(/\b(?:temporada|season|temp(?:\.|orada)?)\s*(\d{1,2})\b/i);
        if (m && m[1]) return Number(m[1]);
        m = seg.match(/\bs(\d{1,2})\b/i);
        if (m && m[1]) return Number(m[1]);
      }
    } catch (e) {
      // ignore
    }
    return null;
  };

  const detectSxxEyy = (p: string): { season: number; episode: number } | null => {
    try {
      const s = String(p || '');
      const m = s.match(/\bS(\d{1,2})E(\d{1,3})\b/i);
      if (!m) return null;
      const season = Number(m[1]);
      const episode = Number(m[2]);
      if (!Number.isFinite(season) || !Number.isFinite(episode)) return null;
      return { season, episode };
    } catch (e) {
      return null;
    }
  };

  const guessEpisodeNumber = (fp: string): number | null => {
    const sxe = detectSxxEyy(fp);
    if (sxe?.episode && Number.isFinite(sxe.episode)) return sxe.episode;
    try {
      const bn = (String(fp || '').split(/[\\/]/).pop() || '').trim();
      const m = bn.match(/^(\d{1,3})\b/);
      if (m && m[1]) {
        const n = Number(m[1]);
        if (Number.isFinite(n)) return n;
      }
    } catch (e) {
      // ignore
    }
    return null;
  };

  const inferSeriesRootGuess = (files: any[]): string | null => {
    try {
      for (const f of files) {
        const fp = String(f?.path || '');
        if (!fp) continue;
        const parts = fp.split(/[\\/]/).filter(Boolean);
        for (let i = parts.length - 2; i >= 0; i--) {
          const seg = String(parts[i] || '').trim();
          const m = seg.match(/\b(?:temporada|season|temp(?:\.|orada)?)\s*(\d{1,2})\b/i) || seg.match(/\bs(\d{1,2})\b/i);
          if (!m || !m[1]) continue;
          const sn = Number(m[1]);
          if (!Number.isFinite(sn) || sn <= 1) continue;
          const rootParts = parts.slice(0, i);
          if (rootParts.length) return rootParts.join('\\');
          break;
        }
      }
    } catch (e) {
      // ignore
    }
    return null;
  };

  const groupFilesBySeason = (files: any[], seasonMetaMap: Map<number, any>) => {
    const dirname = (p: string): string => {
      const parts = String(p || '').split(/[\\/]/).filter(Boolean);
      parts.pop();
      return parts.join('\\');
    };

    const seriesRootGuess = inferSeriesRootGuess(files);
    const hasSeasonHints = (() => {
      try {
        for (const f of files) {
          const fp = String(f?.path || '');
          if (!fp) continue;
          if (detectSxxEyy(fp)) return true;
          if (detectSeasonFromPath(fp) != null) return true;
        }
      } catch (e) {
        // ignore
      }
      return false;
    })();
    const groups = new Map<number | null, any[]>();

    for (const f of files) {
      const fp = String(f?.path || '');
      const sxe = detectSxxEyy(fp);
      let sn: number | null = (sxe?.season ?? detectSeasonFromPath(fp)) ?? null;

      if (sn == null && seriesRootGuess) {
        const parent = dirname(fp);
        const looksLikeRootChild = parent && parent.toLowerCase() === seriesRootGuess.toLowerCase();
        const hasSeason1Meta = seasonMetaMap.has(1);
        if (looksLikeRootChild && (hasSeason1Meta || seasonMetaMap.size >= 2)) {
          sn = 1;
        }
      }

      // If TMDB metadata has seasons but filenames don't include any season hints,
      // assume these belong to Season 1 (common when S1 lives in the root folder).
      if (sn == null && !hasSeasonHints && seasonMetaMap.has(1)) {
        sn = 1;
      }

      if (!groups.has(sn)) groups.set(sn, []);
      groups.get(sn)!.push(f);
    }

    return groups;
  };

  const sortSeasonFiles = (seasonFiles: any[]): any[] => {
    return [...seasonFiles].sort((a, b) => {
      const ap = String(a?.path || '');
      const bp = String(b?.path || '');
      const ae = guessEpisodeNumber(ap);
      const be = guessEpisodeNumber(bp);
      if (ae != null && be != null) return ae - be;
      if (ae != null) return -1;
      if (be != null) return 1;
      return ap.localeCompare(bp);
    });
  };

  const buildPlaylistForSeason = (seasonFiles: any[], seasonNumber: number | null): any[] => {
    const sorted = sortSeasonFiles(seasonFiles);
    const seasonMeta = seasonNumber != null ? seasonMetaMap.get(seasonNumber) : null;
    const seasonEpisodes = (seasonMeta && Array.isArray((seasonMeta as any).episodes)) ? (seasonMeta as any).episodes : null;

    return sorted.map((f, i) => {
      const fp = String(f?.path || '');
      const ep = guessEpisodeNumber(fp) ?? (i + 1);
      const seasonEp = seasonEpisodes
        ? seasonEpisodes.find((e: any) => Number(e?.episode_number) === Number(ep)) || seasonEpisodes[ep - 1]
        : null;
      const episodeTitle = (seasonEp && (seasonEp.title || seasonEp.name)) ? String(seasonEp.title || seasonEp.name) : undefined;

      // Normalize index fields so the player label matches actual episode number.
      return { ...f, index: ep, file_index: ep, episodeTitle, seasonNumber };
    });
  };

  const buildSeasonPlaylists = (files: any[]) => {
    const groups = groupFilesBySeason(files, seasonMetaMap);
    const out: Record<number, any[]> = {};
    for (const [sn, seasonFiles] of groups.entries()) {
      if (sn == null) continue;
      out[sn] = buildPlaylistForSeason(seasonFiles, sn);
    }
    return out;
  };

  const normalizeDbSeasonsForUi = (seasons: any[]) => {
    const entries = (seasons || []).map((s) => ({
      season: s,
      seasonNumber: Number(s?.season_number ?? s?.seasonNumber ?? 1),
    }));

    const specials = entries
      .filter((e) => Number.isFinite(e.seasonNumber) && e.seasonNumber === 0)
      .map((e) => ({ ...e, displayNumber: 0 }));

    const regular = entries.filter((e) => Number.isFinite(e.seasonNumber) && e.seasonNumber > 0);
    const nums = regular.map((e) => e.seasonNumber);
    const hasDup = new Set(nums).size !== nums.length;

    if (!hasDup) {
      return [
        ...specials,
        ...regular.map((e) => ({ ...e, displayNumber: e.seasonNumber })),
      ];
    }

    const sorted = [...regular].sort((a, b) => {
      const ad = a.season?.air_date_start || a.season?.airDateStart || null;
      const bd = b.season?.air_date_start || b.season?.airDateStart || null;
      if (ad && bd && ad !== bd) return String(ad).localeCompare(String(bd));
      const ae = Number(a.season?.tmdb_episode_start ?? a.season?.tmdbEpisodeStart ?? 0);
      const be = Number(b.season?.tmdb_episode_start ?? b.season?.tmdbEpisodeStart ?? 0);
      if (Number.isFinite(ae) && Number.isFinite(be) && ae !== be) return ae - be;
      const ai = Number(a.season?.id ?? 0);
      const bi = Number(b.season?.id ?? 0);
      return ai - bi;
    });

    return [
      ...specials,
      ...sorted.map((e, idx) => ({ ...e, displayNumber: idx + 1 })),
    ];
  };

  const buildSeasonPlaylistsFromDb = (entries: { season: any; displayNumber: number }[]): Record<number, any[]> => {
    const out: Record<number, any[]> = {};
    try {
      for (const entry of entries || []) {
        const s = entry.season;
        const sn = Number(entry.displayNumber ?? 1);
        if (!Number.isFinite(sn)) continue;
        const eps = Array.isArray(s?.episodes) ? s.episodes : [];
        out[sn] = eps.map((e: any) => {
          let epNo = Number(e?.episode_number ?? e?.episodeNumber ?? 0);
          if (Number.isFinite(epNo) && epNo >= 1000) {
            epNo = epNo % 1000;
          }
          const file = e?.file || {};
          const path = String(file?.path || '');
          const filename = String(file?.filename || (path ? path.split(/[\\/]/).pop() : '') || '');
          return {
            ...file,
            path,
            filename,
            index: Number.isFinite(epNo) && epNo > 0 ? epNo : undefined,
            file_index: file?.file_index ?? file?.fileIndex ?? (Number.isFinite(epNo) && epNo > 0 ? epNo : undefined),
            episodeTitle: e?.title || undefined,
            stillPath: e?.still_path ?? e?.stillPath ?? undefined,
            seasonNumber: sn,
          };
        });
      }
    } catch (e) {
      // ignore
    }
    return out;
  };

  const getSeasonMetaMap = () => {
    const out = new Map<number, any>();
    try {
      const seasonsObj =
        metaRaw?.modal?.seasons ||
        metaRaw?.i18n?.[lang]?.seasons ||
        metaRaw?.i18n?.en?.seasons ||
        metaRaw?.i18n?.es?.seasons ||
        null;

      if (!seasonsObj || typeof seasonsObj !== 'object') return out;
      for (const [k, v] of Object.entries(seasonsObj)) {
        const sn = Number(k);
        if (!Number.isFinite(sn)) continue;
        out.set(sn, v);
      }
    } catch (e) {
      // ignore
    }
    return out;
  };

  const seasonMetaMap = getSeasonMetaMap();
  const seasonNumbers = Array.from(seasonMetaMap.keys()).filter(n => Number.isFinite(n)).sort((a, b) => a - b);
  const isTmdb = String(meta?.provider || metaRaw?.provider || '').toLowerCase() === 'tmdb';

  const buildPrimaryPlayItem = (): MediaItem => {
    const base = (detail || item) as any as MediaItem;
    const files = ((detail && (detail as any).files) || (item as any).files || []) as any[];
    const related = ((detail as any)?.related || (item as any)?.related || []) as any[];
    const sequel = Array.isArray(related) ? related.find((r: any) => String(r?.relation || '').toLowerCase() === 'sequel') : null;
    const dbSeasonPlaylists = (() => {
      const db = Array.isArray(dbEpisodeSeasons) ? dbEpisodeSeasons : [];
      if (!db.length) return null;
      const sorted = [...db].sort((a, b) => {
        const an = Number(a?.season_number ?? a?.seasonNumber ?? 0);
        const bn = Number(b?.season_number ?? b?.seasonNumber ?? 0);
        return (Number.isFinite(an) ? an : 0) - (Number.isFinite(bn) ? bn : 0);
      });
      const ui = normalizeDbSeasonsForUi(sorted);
      const playlists = buildSeasonPlaylistsFromDb(ui as any);
      return Object.keys(playlists).length ? playlists : null;
    })();
    const seasonPlaylists = dbSeasonPlaylists || buildSeasonPlaylists(files);

    if (!Array.isArray(files) || files.length === 0) return base;

    if (isSeriesLike && isTmdb && selectedSeason != null) {
      const playlistFromDb = dbSeasonPlaylists ? dbSeasonPlaylists[selectedSeason] : null;
      if (Array.isArray(playlistFromDb) && playlistFromDb.length) {
        const first = playlistFromDb[0];
        const firstPath = first?.path ? String(first.path) : '';
        if (firstPath) {
          return {
            ...(base as any),
            path: firstPath,
            files: playlistFromDb as any,
            nextSeries: sequel || null,
            seasonPlaylists,
            selectedSeason,
          } as any;
        }
      }
      const groups = groupFilesBySeason(files, seasonMetaMap);
      const seasonFiles = groups.get(selectedSeason) || [];
      const playlistFiles = buildPlaylistForSeason(seasonFiles, selectedSeason);
      const first = playlistFiles[0];
      const firstPath = first?.path ? String(first.path) : '';
      if (firstPath) {
        return {
          ...(base as any),
          path: firstPath,
          files: playlistFiles as any,
          nextSeries: sequel || null,
          seasonPlaylists,
          selectedSeason,
        } as any;
      }
    }

    if (base.path) return base;
    const first = files[0];
    const firstPath = first?.path ? String(first.path) : '';
    return firstPath
      ? ({
          ...(base as any),
          path: firstPath,
          files: files as any,
          nextSeries: sequel || null,
          seasonPlaylists,
          selectedSeason,
        } as any)
      : base;
  };

  useEffect(() => {
    // Default: if TMDB has multiple seasons, pick Season 1 (or first available).
    if (!isTmdb) {
      setSelectedSeason(null);
      return;
    }
    if (!seasonNumbers.length) {
      setSelectedSeason(null);
      return;
    }
    if (seasonNumbers.length >= 2) {
      setSelectedSeason((prev) => {
        if (prev && seasonNumbers.includes(prev)) return prev;
        return seasonNumbers.includes(1) ? 1 : (seasonNumbers[0] || null);
      });
      return;
    }
    // Single-season show: no selector needed.
    setSelectedSeason(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.id, detail?.id, isTmdb]);

  const coerceDate = (d: any): string | null => {
    if (!d) return null;
    if (typeof d === 'string') {
      const s = d.trim();
      if (!s) return null;
      // common shapes: "YYYY-MM-DD", or ISO with time
      if (s.length >= 10 && /^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
      return s;
    }
    return null;
  };

  const firstAirDate =
    coerceDate(metaRaw?.first_air_date) ||
    coerceDate(metaRaw?.release_date) ||
    coerceDate(metaRaw?.aired?.from) ||
    coerceDate(metaRaw?.aired?.prop?.from?.string) ||
    null;

  const lastAirDate =
    coerceDate(metaRaw?.last_air_date) ||
    coerceDate(metaRaw?.aired?.to) ||
    coerceDate(metaRaw?.aired?.prop?.to?.string) ||
    null;

  const airStatus: string | null = (() => {
    const s = metaRaw?.status;
    if (!s) return null;
    try {
      return String(s);
    } catch (e) {
      return null;
    }
  })();

  const primaryGenres = (detail && detail.genre) || item.genre || [];

  const localizedMeta = (() => {
    if (!metaRaw || typeof metaRaw !== 'object') return null;
    const obj = metaRaw?.i18n?.[lang] || metaRaw?.i18n?.en || metaRaw?.i18n?.es || null;
    return obj && typeof obj === 'object' ? obj : null;
  })();

  const localizedSynopsis =
    localizedMeta?.synopsis ||
    localizedMeta?.overview ||
    (detail && detail.overview) ||
    item.overview ||
    t('no_description');

  const selectedDbSeason = (() => {
    try {
      const dbSeasons = Array.isArray(dbEpisodeSeasons) ? dbEpisodeSeasons : null;
      if (!dbSeasons || !dbSeasons.length) return null;
      if (selectedSeason != null) {
        const match = dbSeasons.find((s: any) => Number(s?.season_number ?? s?.seasonNumber) === Number(selectedSeason));
        if (match) return match;
      }
      if (dbSeasons.length === 1) return dbSeasons[0];
      return null;
    } catch {
      return null;
    }
  })();

  const seasonSynopsis = (() => {
    try {
      const s = selectedDbSeason?.synopsis_localized ?? selectedDbSeason?.synopsisLocalized ?? null;
      if (typeof s === 'string' && s.trim()) return s.trim();
    } catch {
      return null;
    }
    return null;
  })();

  const resolvedSynopsis = seasonSynopsis || localizedSynopsis;

  const seasonPoster = (() => {
    try {
      const p = selectedDbSeason?.poster_path ?? selectedDbSeason?.posterPath ?? null;
      if (typeof p === 'string' && p.trim()) return resolveAssetUrl(p.trim());
    } catch {
      return null;
    }
    return null;
  })();

  const seasonBackdrop = (() => {
    try {
      const p = selectedDbSeason?.backdrop_path ?? selectedDbSeason?.backdropPath ?? null;
      if (typeof p === 'string' && p.trim()) return resolveAssetUrl(p.trim());
    } catch {
      return null;
    }
    return null;
  })();

  const stripCache = (u: string | null): string | null => {
    if (!u) return null;
    const s = String(u).trim();
    if (!s) return null;
    const q = s.indexOf('?');
    return q >= 0 ? s.slice(0, q) : s;
  };
  const isLocalAsset = (u: string | null): boolean => {
    const s = stripCache(u);
    return !!(s && s.startsWith('/media-assets/'));
  };

  const itemBackdrop = (() => {
    try {
      const p = (detail as any)?.backdropUrl || item.backdropUrl || null;
      if (typeof p === 'string' && p.trim()) return p.trim();
    } catch {
      return null;
    }
    return null;
  })();

  const seasonBackdropLooksLikePoster = (() => {
    const b = stripCache(seasonBackdrop);
    const p = stripCache(seasonPoster);
    return !!(b && p && b === p);
  })();

  const related = (() => {
    const rel = ((detail as any)?.related || (item as any)?.related || []) as any[];
    if (!Array.isArray(rel)) return [];
    const filtered = rel.filter((r: any) => r && r.id && r.title);
    const rank = (r: any) => {
      const relName = String(r?.relation || '').toLowerCase();
      if (relName === 'prequel') return 0;
      if (relName === 'sequel') return 1;
      return 2;
    };
    return filtered.sort((a: any, b: any) => rank(a) - rank(b));
  })();

  const heroBackdrop =
    // Prefer local backdrop if available on item/detail. This avoids using
    // remote season fallbacks where backdrop was copied from poster.
    ((isLocalAsset(itemBackdrop) && (!seasonBackdrop || !isLocalAsset(seasonBackdrop) || seasonBackdropLooksLikePoster))
      ? itemBackdrop
      : null) ||
    seasonBackdrop ||
    seasonPoster ||
    itemBackdrop ||
    item.thumbnailUrl ||
    fallbackBackdrop;
  const heroHasBackdrop = !!(
    heroBackdrop &&
    heroBackdrop !== fallbackBackdrop &&
    (
      seasonBackdrop ||
      seasonPoster ||
      itemBackdrop ||
      item.thumbnailUrl
    )
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-1.5 sm:p-4 lg:p-6 animate-fade-in">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/70 backdrop-blur-md transition-opacity" onClick={onClose} />
      
      {/* Modal Content */}
      <div className="relative w-[99%] sm:w-[96%] max-w-7xl mx-auto my-[4px] sm:my-0 bg-[#0f172a] rounded-[22px] sm:rounded-[32px] overflow-hidden overflow-y-auto shadow-[0_50px_150px_rgba(0,0,0,0.8)] border border-white/10 flex flex-col max-h-[96vh] animate-slide-up custom-scrollbar">
        
        {/* Close Button */}
        <button 
          onClick={onClose}
          className="absolute top-3 right-3 sm:top-4 sm:right-4 z-30 w-10 h-10 sm:w-12 sm:h-12 rounded-full bg-black/50 hover:bg-indigo-600 text-white backdrop-blur-md border border-white/10 transition-all flex items-center justify-center group"
        >
          <X size={24} className="group-hover:scale-110 transition-transform" />
        </button>

        {/* Hero Section */}
        <div className="relative w-full h-[240px] sm:h-[42vh] lg:h-[56vh] min-h-[220px] overflow-hidden rounded-t-[22px] sm:rounded-t-[32px] bg-[#05070c] shrink-0">
          <img 
            src={heroBackdrop} 
            alt={item.title}
            className="w-full h-full object-cover object-top sm:object-[center_20%] animate-in fade-in duration-1000 scale-105" 
          />
          {!heroHasBackdrop && (
            <div className="absolute inset-0 flex items-center justify-center text-slate-200/70 bg-slate-900">
              <div className="flex flex-col items-center gap-3">
                <div className="p-6 rounded-3xl bg-white/5 border border-white/10 backdrop-blur-xl">
                  <ImageOff size={40} className="text-slate-500" />
                </div>
                <span className="text-xs font-black uppercase tracking-[0.2em] text-slate-500">{t('no_image')}</span>
              </div>
            </div>
          )}
          
          {/* Enhanced Layered Gradients */}
          <div className="absolute inset-0 bg-gradient-to-t from-[#0f172a] via-[#0f172a]/60 to-transparent" />
          <div className="absolute inset-0 bg-gradient-to-r from-[#0f172a]/80 via-transparent to-transparent hidden sm:block" />
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_bottom_left,rgba(99,102,241,0.25),transparent_60%)]" />
          
          <div className="absolute bottom-0 left-0 right-0 p-4 sm:p-10 lg:p-16 z-10">
            <div className="max-w-5xl">
              <h2 className="text-2xl sm:text-5xl lg:text-6xl font-black text-white mb-4 sm:mb-6 drop-shadow-[0_20px_50px_rgba(0,0,0,0.7)] leading-[1.1] tracking-tight animate-in fade-in slide-in-from-left-4 duration-700">
                {detail?.title || item.title}
              </h2>
              
              <div className="flex items-center flex-wrap gap-2 mb-6 animate-in fade-in slide-in-from-left-6 duration-1000 delay-75">
                {((detail as any)?.is_identified === false || (item as any)?.is_identified === false) && (
                  <span className="px-2 py-0.5 rounded-md bg-amber-500 text-black text-[10px] font-black uppercase tracking-wider shadow-lg">
                    {t('local_badge')}
                  </span>
                )}
                {((detail && detail.rating) || item.rating) && (
                  <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-emerald-500/20 border border-emerald-500/30 text-emerald-400 text-xs font-black">
                    <ThumbsUp size={12} fill="currentColor" />
                    {Math.round(((detail && detail.rating) || item.rating) * 10)}%
                  </div>
                )}
                <span className="text-slate-200 text-sm font-bold">{(detail && detail.year) || item.year}</span>
                <span className="w-1 h-1 rounded-full bg-slate-600" />
                <span className="px-1.5 py-0.5 bg-white/10 border border-white/10 rounded text-[10px] font-black text-slate-200 uppercase tracking-widest">4K ULTRA HD</span>
                <span className="w-1 h-1 rounded-full bg-slate-600" />
                <span className="text-slate-200 text-sm font-bold capitalize">{(detail && detail.type) || item.type}</span>
              </div>

              <div className="flex flex-wrap items-center gap-3 animate-in fade-in slide-in-from-bottom-4 duration-1000 delay-200">
                <Button 
                  onClick={() => onPlay(buildPrimaryPlayItem())}
                  size="lg" 
                  icon={<Play size={24} fill="currentColor" />}
                  className="h-12 sm:h-14 px-8 sm:px-10 bg-white text-black font-black text-base sm:text-lg rounded-2xl shadow-[0_20px_40px_rgba(255,255,255,0.25)] hover:bg-indigo-50 hover:scale-105 active:scale-95 transition-all duration-300 flex-1 sm:flex-none justify-center"
                >
                  {t('play')}
                </Button>

                <div className="flex gap-2 flex-1 sm:flex-none">
                  <button 
                    onClick={async () => {
                      if (!profileId || listBusy) return;
                      setListBusy(true);
                      try {
                        const res = await MediaService.toggleUserListItem(Number(item.id), profileId);
                        setInUserList(!!res?.in_list);
                        if (onListChanged) onListChanged();
                      } catch {
                        // ignore
                      } finally {
                        setListBusy(false);
                      }
                    }}
                    className={`h-12 w-12 sm:h-14 sm:w-14 rounded-2xl flex items-center justify-center transition-all duration-300 border backdrop-blur-xl active:scale-90 ${inUserList ? 'bg-indigo-600 border-indigo-500 text-white shadow-[0_10px_25px_rgba(79,70,229,0.4)]' : 'bg-white/10 border-white/10 text-white hover:bg-white/20'}`}
                    title={inUserList ? t('in_my_list') : t('my_list')}
                  >
                    {inUserList ? <Check size={24} /> : <Plus size={24} />}
                  </button>

                  {isAdmin && (
                    <>
                      <button
                        onClick={handleRefreshMetadata}
                        disabled={refreshingMeta}
                        className="h-12 w-12 sm:h-14 sm:w-14 rounded-2xl flex items-center justify-center bg-white/10 border border-white/10 text-white backdrop-blur-xl hover:bg-white/20 transition-all active:scale-90 disabled:opacity-50"
                        title={t('update_metadata')}
                      >
                        <RefreshCw size={24} className={refreshingMeta ? 'animate-spin' : ''} />
                      </button>
                      <button
                        onClick={() => setShowManualMapping(true)}
                        className="h-12 w-12 sm:h-14 sm:w-14 rounded-2xl flex items-center justify-center bg-white/10 border border-white/10 text-white backdrop-blur-xl hover:bg-white/20 transition-all active:scale-90"
                        title={t('fix_match')}
                      >
                        <Wrench size={24} />
                      </button>
                    </>
                  )}
                </div>
              </div>

              {refreshMsg ? (
                <div className={`mt-4 px-4 py-2 rounded-xl text-xs font-bold inline-block backdrop-blur-md animate-in zoom-in-95 duration-300 ${refreshMsg.toLowerCase().includes('fail') || refreshMsg.toLowerCase().includes('error') ? 'bg-red-500/20 text-red-300 border border-red-500/30' : 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'}`}>
                  {refreshMsg}
                </div>
              ) : null}
            </div>
          </div>
        </div>

        {/* Content Section */}
        <div className="flex-1 px-4 sm:px-8 lg:px-16 py-6 sm:py-10 lg:py-12 bg-gradient-to-b from-[#0f172a] to-[#05070c]">
          <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-6 sm:gap-10 lg:gap-16">
            <div className="space-y-6">
              <div className="rounded-[24px] overflow-hidden border border-white/10 bg-white/5 shadow-[0_30px_60px_rgba(0,0,0,0.5)] group">
                <img
                  src={seasonPoster || (detail as any)?.posterPath || (item as any).posterPath || fallbackPoster}
                  alt={item.title}
                  className="w-full aspect-[2/3] object-cover transition-transform duration-700 group-hover:scale-110"
                />
              </div>
              
              {/* Secondary Technical Stats for Desktop */}
              <div className="hidden lg:block space-y-4 p-6 rounded-3xl bg-white/[0.02] border border-white/5">
                <div className="flex justify-between items-center">
                  <span className="text-[10px] font-black text-slate-500 uppercase tracking-widest">{t('format')}</span>
                  <span className="text-xs font-bold text-white bg-indigo-500/20 px-2 py-0.5 rounded border border-indigo-500/30">4K HDR</span>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-[10px] font-black text-slate-500 uppercase tracking-widest">{t('audio')}</span>
                  <span className="text-xs font-bold text-slate-300">5.1 Surround</span>
                </div>
              </div>
            </div>

            <div className="space-y-7 sm:space-y-10">
                <div className="prose prose-invert max-w-none">
                  <h3 className="text-xl font-black text-white mb-4 uppercase tracking-widest flex items-center gap-3">
                    <div className="w-1 h-6 bg-indigo-500 rounded-full" />
                    {t('synopsis')}
                  </h3>
                  <p className="text-base sm:text-xl text-slate-300 leading-relaxed font-medium">
                    {resolvedSynopsis}
                  </p>
                </div>

              {/* Detalle Técnico Unificado */}
              <div className="bg-white/5 rounded-[32px] border border-white/10 overflow-hidden shadow-[inset_0_1px_1px_rgba(255,255,255,0.05)] backdrop-blur-sm">
                <div className="grid grid-cols-1 lg:grid-cols-2 divide-y lg:divide-y-0 lg:divide-x divide-white/10">
                  <div className="p-8 space-y-6 bg-white/[0.01]">
                    <div className="flex justify-between items-center border-b border-white/5 pb-4">
                      <span className="text-[11px] font-black text-indigo-400 uppercase tracking-[0.2em] flex items-center gap-2">
                        <Calendar size={14} /> {t('release_year')}
                      </span>
                      <span className="text-base font-black text-white">{(detail && (detail as any).releaseYear) || (item as any).releaseYear || (item.year || '—')}</span>
                    </div>
                    <div className="flex justify-between items-center border-b border-white/5 pb-4">
                      <span className="text-[11px] font-black text-indigo-400 uppercase tracking-[0.2em] flex items-center gap-2">
                        <Clock size={14} /> {t('duration')}
                      </span>
                      <span className="text-base font-black text-white">
                        {(() => {
                          const v = (detail && (detail as any).runtime) ?? (item as any).runtime ?? (detail && (detail as any).duration) ?? item.duration ?? null;
                          const n = v == null ? NaN : Number(v);
                          return (Number.isFinite(n) && n > 0) ? `${Math.round(n)} min` : '—';
                        })()}
                      </span>
                    </div>
                    {airStatus && (
                      <div className="flex justify-between items-center">
                        <span className="text-[11px] font-black text-indigo-400 uppercase tracking-[0.2em]">{t('status')}</span>
                        <span className="px-3 py-1 rounded-full bg-emerald-500/10 text-emerald-400 text-[11px] font-black border border-emerald-500/20 shadow-lg shadow-emerald-500/5">{airStatus}</span>
                      </div>
                    )}
                  </div>

                  <div className="p-8 space-y-6 hidden lg:block">
                    <div className="flex justify-between items-center border-b border-white/5 pb-4">
                      <span className="text-[11px] font-black text-indigo-400 uppercase tracking-[0.2em]">{t('genres')}</span>
                      <div className="flex flex-wrap gap-2 justify-end">
                        {((detail && detail.genre) || item.genre)?.slice(0, 3).map((g: string) => (
                          <span key={g} className="px-3 py-1 rounded-lg bg-white/5 border border-white/10 text-[11px] font-bold text-slate-300">{g}</span>
                        )) || '—'}
                      </div>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="text-[11px] font-black text-indigo-400 uppercase tracking-[0.2em]">MAL ID</span>
                      <span className="text-sm font-mono font-bold text-slate-400 bg-black/30 px-3 py-1 rounded-lg border border-white/5">{(detail && (detail as any).malId) || (item as any).malId || '—'}</span>
                    </div>
                  </div>
                </div>
              </div>

              {/* Related Section (Moved from sidebar) */}
              {related.length ? (
                <div className="mt-8">
                  <h4 className="text-lg font-bold text-white mb-4">{t('related')}</h4>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    {related.map((r: any) => (
                      <div key={r.id} className="flex items-center gap-4 p-3 rounded-xl bg-white/5 border border-white/10 hover:bg-white/10 transition-colors group">
                        <div className="w-16 h-24 rounded-lg overflow-hidden bg-slate-800 flex-shrink-0 shadow-lg border border-white/5">
                          <img src={resolveAssetUrl(r.poster_url) || item.thumbnailUrl || item.backdropUrl || fallbackPoster} className="w-full h-full object-cover transition-transform group-hover:scale-105" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="text-sm font-bold text-white truncate group-hover:text-indigo-400 transition-colors">{r.title}</div>
                          <div className="text-[10px] text-slate-500 uppercase tracking-widest mt-1">{r.relation || t('related_relation')}</div>
                          <button
                            onClick={() => {
                              onClose();
                              const nextItem = { ...(detail || item), id: r.id } as any;
                              onPlay(nextItem);
                            }}
                            className="mt-2 text-[11px] font-bold text-indigo-400 hover:text-indigo-300 flex items-center gap-1 transition-colors"
                          >
                            {t('view')} <Plus size={10} />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}

              {/* Season Selector (Moved from sidebar) */}
              {isTmdb && seasonNumbers.length >= 2 ? (
                <div className="mt-8 p-6 rounded-2xl bg-indigo-500/5 border border-indigo-500/10">
                  <div className="flex items-center gap-2 text-sm font-bold text-indigo-300 uppercase tracking-wider mb-4">
                    <Calendar size={16} />
                    {t('seasons')}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {seasonNumbers.map((sn) => {
                      const active = selectedSeason === sn;
                      return (
                        <button
                          key={sn}
                          onClick={() => setSelectedSeason(sn)}
                          className={`min-w-[40px] h-10 px-4 rounded-xl text-sm font-bold border transition-all ${
                            active
                              ? 'bg-indigo-600 text-white border-indigo-500 shadow-[0_0_20px_rgba(79,70,229,0.4)] scale-110'
                              : 'bg-white/5 text-slate-300 border-white/10 hover:bg-white/10 hover:border-white/20'
                          }`}
                        >
                          {sn}
                        </button>
                      );
                    })}
                  </div>
                  <p className="mt-3 text-xs text-slate-500 italic">
                    {t('select_season_hint')}
                  </p>
                </div>
              ) : null}

              {/* Episodes Section (Mockup if series) */}
                 {isSeriesLike && (
                 <div className="mt-8">
                  <h3 className="text-xl font-bold text-white mb-4">{t('episodes')}</h3>
                  {(() => {
                    const files = ((detail && (detail as any).files) || (item as any).files || []) as any[];
                    const dbSeasons = Array.isArray(dbEpisodeSeasons) ? dbEpisodeSeasons : null;
                    if (dbEpisodeLoading) {
                      return <InlineLoader label={t('loading')} />;
                    }

                    // Prefer JOINed DB seasons/episodes (do not depend on metadata blobs).
                    if (dbSeasons && dbSeasons.length) {
      const sortedSeasons = [...dbSeasons].sort((a, b) => {
        const an = Number(a?.season_number ?? a?.seasonNumber ?? 0);
        const bn = Number(b?.season_number ?? b?.seasonNumber ?? 0);
        return (Number.isFinite(an) ? an : 0) - (Number.isFinite(bn) ? bn : 0);
      });
      const uiSeasons = normalizeDbSeasonsForUi(sortedSeasons);
      const seasonNums = Array.from(
        new Set(
          uiSeasons
            .map(s => Number(s?.displayNumber ?? 1))
            .filter(n => Number.isFinite(n)) as number[]
        )
      ).sort((a, b) => a - b);
      const useSeasonSelector = seasonNums.length >= 2;
      const visible = useSeasonSelector && selectedSeason != null ? [selectedSeason] : seasonNums;
      const playlists = buildSeasonPlaylistsFromDb(uiSeasons as any);

                      return (
                        <div className="space-y-6">
                          {useSeasonSelector ? (
                            <div className="flex items-center justify-between">
                              <span className="text-sm text-slate-400">{t('season')}</span>
                              <select
                                value={selectedSeason ?? seasonNums[0]}
                                onChange={(e) => setSelectedSeason(Number(e.target.value))}
                                className="bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-indigo-500/40"
                              >
                                {seasonNums.map((sn) => (
                                  <option key={sn} value={sn}>{`${t('season')} ${sn}`}</option>
                                ))}
                              </select>
                            </div>
                          ) : null}

                          {visible.map((sn) => {
                            const playlistFiles = playlists[sn] || [];
                            if (!playlistFiles.length) {
                              return (
                                <div key={`db-s${sn}`} className="text-sm text-slate-500">
                                  {t('no_episodes_season')}
                                </div>
                              );
                            }
                            const rangeKey = `s${sn}`;
                            const rangeSize = 50;
                            const totalRanges = Math.ceil(playlistFiles.length / rangeSize);
                            const currentStart = Math.max(0, episodeRanges[rangeKey] ?? 0);
                            const currentRangeIndex = Math.floor(currentStart / rangeSize);
                            const visibleFiles = totalRanges > 1
                              ? playlistFiles.slice(currentStart, currentStart + rangeSize)
                              : playlistFiles;

                            return (
                              <div key={`db-s${sn}`} className="space-y-2">
                                <div className="flex items-center justify-between">
                                  <h4 className="text-base font-semibold text-white">{`${t('season')} ${sn}`}</h4>
                                  <span className="text-xs text-slate-500">{playlistFiles.length} {t('files')}</span>
                                </div>

                                {totalRanges > 1 ? (
                                  <div className="flex items-center justify-between gap-3">
                                    <span className="text-xs text-slate-500">{`${currentRangeIndex * rangeSize + 1}-${Math.min((currentRangeIndex + 1) * rangeSize, playlistFiles.length)}`}</span>
                                    <div className="flex items-center gap-2">
                                      <button
                                        className="px-2 py-1 text-xs rounded-md bg-white/5 hover:bg-white/10 text-slate-200 disabled:opacity-40"
                                        disabled={currentStart <= 0}
                                        onClick={() => setEpisodeRanges((prev) => ({ ...prev, [rangeKey]: Math.max(0, currentStart - rangeSize) }))}
                                      >
                                        {t('previous')}
                                      </button>
                                      <button
                                        className="px-2 py-1 text-xs rounded-md bg-white/5 hover:bg-white/10 text-slate-200 disabled:opacity-40"
                                        disabled={currentStart + rangeSize >= playlistFiles.length}
                                        onClick={() => setEpisodeRanges((prev) => ({ ...prev, [rangeKey]: currentStart + rangeSize }))}
                                      >
                                        {t('next')}
                                      </button>
                                    </div>
                                  </div>
                                ) : null}

                                <div className="grid grid-cols-1 gap-3">
                                  {visibleFiles.map((f: any, i: number) => {
                                      const fp = String(f?.path || '');
                                      const basePoster = (detail as any)?.posterPath || (item as any).posterPath || fallbackPoster;
                                      const episodeThumb = resolveAssetUrl(
                                        (f?.stillPath || f?.still_path || (detail as any)?.backdropUrl || item.backdropUrl || basePoster) as string,
                                      );
                                      const epNo = f?.index ?? f?.file_index ?? (currentStart + i + 1);
                                      const episodeWord = t('episode');
                                      const title = f?.episodeTitle ? String(f.episodeTitle) : `${episodeWord} ${epNo}`;
                                      const isWatched = (f as any).watched === true;

                                      return (
                                        <div 
                                          key={fp || i} 
                                          className="group flex items-center gap-3 sm:gap-4 bg-white/5 border border-white/5 rounded-2xl p-2.5 sm:p-4 hover:bg-white/10 hover:border-white/10 transition-all duration-300 cursor-pointer" 
                                          onClick={() => {
                                            if (!f || !f.path) return;
                                            const playItem: MediaItem = {
                                              ...(detail || item),
                                              path: f.path,
                                              files: playlistFiles as any,
                                              nextSeries: (detail as any)?.related?.find((r: any) => String(r?.relation || '').toLowerCase() === 'sequel') || null,
                                              seasonPlaylists: buildSeasonPlaylistsFromDb(uiSeasons as any),
                                              selectedSeason: sn,
                                            } as any;
                                            onPlay(playItem);
                                          }}
                                        >
                                          <div className="relative w-16 h-16 sm:w-24 sm:h-24 rounded-xl overflow-hidden bg-slate-800 flex-shrink-0 shadow-lg group-hover:scale-105 transition-transform duration-500">
                                            <img src={episodeThumb} className="w-full h-full object-cover opacity-80 group-hover:opacity-100 transition-opacity" />
                                            <div className="absolute inset-0 bg-black/20 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                                              <div className="w-10 h-10 rounded-full bg-white text-black flex items-center justify-center shadow-xl">
                                                <Play size={20} fill="currentColor" />
                                              </div>
                                            </div>
                                            {isWatched && (
                                              <div className="absolute top-1 right-1 p-1 rounded-md bg-indigo-600 text-white shadow-lg">
                                                <Check size={12} />
                                              </div>
                                            )}
                                          </div>

                                          <div className="flex-1 min-w-0">
                                            <div className="flex items-center gap-2 mb-1">
                                              <span className="text-[10px] font-black text-indigo-400 uppercase tracking-widest bg-indigo-500/10 px-1.5 py-0.5 rounded">EP {epNo}</span>
                                              {f?.size && (
                                                <span className="text-[10px] font-bold text-slate-500">{Math.round((f.size || 0) / 1048576)} MB</span>
                                              )}
                                            </div>
                                            <h4 className="text-white text-xs sm:text-lg font-bold mb-1 line-clamp-2 sm:line-clamp-1 group-hover:text-indigo-300 transition-colors">{title}</h4>
                                            <p className="text-[10px] sm:text-xs text-slate-500 font-mono line-clamp-1 opacity-60 group-hover:opacity-100 transition-opacity">{fp.split(/[\\/]/).pop()}</p>
                                          </div>

                                          <div className="hidden sm:flex items-center justify-center w-12 h-12 rounded-full border border-white/5 group-hover:border-indigo-500/50 group-hover:bg-indigo-500/10 transition-all duration-300">
                                            <Play size={20} className="text-slate-400 group-hover:text-indigo-400 transform translate-x-0.5" />
                                          </div>
                                        </div>
                                      );
                                    })}
                                  </div>
                              </div>
                            );
                          })}
                        </div>
                      );
                    }

                    if (!Array.isArray(files) || files.length === 0) {
                      return <div className="text-sm text-slate-500">{t('no_files')}</div>;
                    }

                    const groups = groupFilesBySeason(files, seasonMetaMap);

                    const sortedKeys = Array.from(groups.keys()).sort((a, b) => {
                      if (a == null && b == null) return 0;
                      if (a == null) return 1;
                      if (b == null) return -1;
                      return a - b;
                    });

                    const useSeasonSelector = isTmdb && seasonNumbers.length >= 2;
                    const visibleKeys = useSeasonSelector && selectedSeason != null
                      ? [selectedSeason]
                      : sortedKeys;

                    return (
                      <div className="space-y-6">
                        {visibleKeys.map((sn) => {
                          const seasonFiles = groups.get(sn) || [];
                          const playlistFiles = buildPlaylistForSeason(seasonFiles, sn);
                          const sortedSeasonFiles = playlistFiles;
                          const seasonMeta = sn != null ? seasonMetaMap.get(sn) : null;
                          const seasonTitle = sn == null
                            ? (sortedKeys.length === 1 ? t('episodes') : t('unknown_season'))
                            : (seasonMeta?.title || seasonMeta?.name || `${t('season')} ${sn}`);
                          const seasonSynopsis = sn != null ? (seasonMeta?.synopsis || seasonMeta?.overview || null) : null;
                          const rangeKey = sn == null ? 'none' : `s${sn}`;
                          const rangeSize = 50;
                          const totalRanges = Math.ceil(sortedSeasonFiles.length / rangeSize);
                          const currentStart = Math.max(0, episodeRanges[rangeKey] ?? 0);
                          const currentRangeIndex = Math.floor(currentStart / rangeSize);
                          const visibleFiles = totalRanges > 1
                            ? sortedSeasonFiles.slice(currentStart, currentStart + rangeSize)
                            : sortedSeasonFiles;

                          return (
                            <div key={sn == null ? 'none' : `s${sn}`} className="space-y-2">
                              {(!useSeasonSelector && sortedKeys.length > 1) ? (
                                <div className="flex items-center justify-between">
                                  <h4 className="text-base font-semibold text-white">{seasonTitle}</h4>
                                  <span className="text-xs text-slate-500">{sortedSeasonFiles.length} {t('files')}</span>
                                </div>
                              ) : null}
                              {(useSeasonSelector && sn != null) ? (
                                <div className="flex items-center justify-between">
                                  <h4 className="text-base font-semibold text-white">{seasonTitle}</h4>
                                  <span className="text-xs text-slate-500">{sortedSeasonFiles.length} {t('files')}</span>
                                </div>
                              ) : null}
                              {seasonSynopsis ? (
                                <div className="text-xs text-slate-400 line-clamp-3">{String(seasonSynopsis)}</div>
                              ) : null}
                              {totalRanges > 1 ? (
                                <div className="flex flex-wrap gap-2 pt-1">
                                  {Array.from({ length: totalRanges }).map((_, ri) => {
                                    const start = ri * rangeSize + 1;
                                    const end = Math.min((ri + 1) * rangeSize, sortedSeasonFiles.length);
                                    const active = ri === currentRangeIndex;
                                    return (
                                      <button
                                        key={`${rangeKey}-${ri}`}
                                        onClick={() =>
                                          setEpisodeRanges((prev) => ({
                                            ...prev,
                                            [rangeKey]: ri * rangeSize,
                                          }))
                                        }
                                        className={`px-3 py-1.5 rounded-lg text-[11px] border transition-colors ${
                                          active
                                            ? 'bg-indigo-600 text-white border-indigo-500/40'
                                            : 'bg-white/5 text-slate-200 border-white/10 hover:bg-white/10'
                                        }`}
                                        title={`${start}-${end}`}
                                      >
                                        {start}-{end}
                                      </button>
                                    );
                                  })}
                                </div>
                              ) : null}
                              <div className="space-y-2">
                                {visibleFiles.map((f, i) => {
                                  const fp = String(f?.path || '');
                                  const poster = (detail && (detail as any).posterPath) || (item as any).posterPath || item.thumbnailUrl || item.backdropUrl || fallbackPoster;
                                  const baseName = fp.split(/[\\/]/).pop() || '';
                                  const sxe = detectSxxEyy(fp);
                                  const globalIndex = currentStart + i;
                                  
                                  // Preferir el índice que ya viene del objeto f (calculado en buildSeasonPlaylistsFromDb)
                                  const epNo = f?.file_index ?? f?.index ?? sxe?.episode ?? (guessEpisodeNumber(fp) ?? (globalIndex + 1));

                                  const seasonMeta = sn != null ? seasonMetaMap.get(sn) : null;
                                  const seasonEpisodes = (seasonMeta && Array.isArray((seasonMeta as any).episodes)) ? (seasonMeta as any).episodes : null;
                                  const seasonEp = seasonEpisodes ? seasonEpisodes.find((e: any) => Number(e?.episode_number) === Number(epNo)) || seasonEpisodes[epNo - 1] : null;

                                  const fallbackEp = meta && Array.isArray((meta as any).episodes) ? (meta as any).episodes[globalIndex] : null;
                                  const title =
                                    (seasonEp && (seasonEp.title || seasonEp.name)) ||
                                    (fallbackEp && (fallbackEp.title || fallbackEp.name)) ||
                                    f?.filename ||
                                    baseName ||
                                    `${t('file')} ${i + 1}`;

                                  return (
                                    <div key={fp || i} className="flex items-center p-2 sm:p-3 rounded-lg hover:bg-white/5 transition-colors cursor-pointer group">
                                      <span className="text-base sm:text-xl font-bold text-slate-600 mr-3 sm:mr-4 group-hover:text-white">{epNo}</span>
                                      <div className="w-32 h-20 rounded bg-slate-800 mr-4 overflow-hidden relative hidden sm:block">
                                        <img src={poster} className="w-full h-full object-cover" />
                                        <div className="absolute inset-0 bg-black/40 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                                          <Play size={20} fill="white" className="text-white" />
                                        </div>
                                      </div>
                                      <div className="flex-1 min-w-0">
                                        <h4 className="text-white text-sm sm:text-base font-medium mb-0.5 line-clamp-1 sm:line-clamp-2">{title}</h4>
                                        <p className="text-[10px] sm:text-xs text-slate-400 line-clamp-1 sm:line-clamp-2">{fp}</p>
                                      </div>
                                      <div className="flex items-center gap-3">
                                        <span className="hidden sm:inline text-sm text-slate-500 mr-2">{Math.round((f?.size || 0) / 1048576)} MB</span>
                                        <Button
                                          size="sm"
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            if (!f || !f.path) return;
                                            const playItem: MediaItem = {
                                              ...(detail || item),
                                              path: f.path,
                                              files: playlistFiles as any,
                                              nextSeries: (detail as any)?.related?.find((r: any) => String(r?.relation || '').toLowerCase() === 'sequel') || null,
                                              seasonPlaylists: buildSeasonPlaylists(files),
                                              selectedSeason: sn ?? null,
                                            } as any;
                                            onPlay(playItem);
                                          }}
                                          icon={<Play size={14} />}
                                        >
                                          <span className="sr-only">{t('play')}</span>
                                          <span className="hidden sm:inline">{t('play')}</span>
                                        </Button>
                                      </div>
                                    </div>
                                  );
                                })}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    );
                  })()}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {showManualMapping ? (
        <ManualMappingModal
          item={detail || item}
          onClose={() => setShowManualMapping(false)}
          onSaved={async () => {
            try {
              const id = Number(item.id);
              const d = await MediaService.getById(id);
              const bustCache = (url: string | undefined) => {
                if (!url) return url;
                const sep = url.includes('?') ? '&' : '?';
                return `${url}${sep}_t=${Date.now()}`;
              };
              if (d) {
                (d as any).posterPath = bustCache((d as any).posterPath);
                (d as any).thumbnailUrl = bustCache((d as any).thumbnailUrl);
                (d as any).backdropUrl = bustCache((d as any).backdropUrl);
              }
              setDetail(d);
              if (onManualMappingSaved) onManualMappingSaved(String(id));
            } catch {
              // ignore
            }
          }}
        />
      ) : null}
    </div>
  );
};
