import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MediaItem, UserProfile } from '../types';
import { MediaService } from '../services/api';

interface UsePlayerStateParams {
  currentUser: UserProfile | null;
  lang: string;
  recordPlay: (id: string | number | null | undefined) => void;
  onCloseDetail?: () => void;
}

export const usePlayerState = ({
  currentUser,
  lang,
  recordPlay,
  onCloseDetail,
}: UsePlayerStateParams) => {
  const [playingMedia, setPlayingMedia] = useState<MediaItem | null>(null);
  const [playingSrc, setPlayingSrc] = useState<string | null>(null);
  const [playingStartTime, setPlayingStartTime] = useState<number>(0);
  const [playerSeasons, setPlayerSeasons] = useState<
    Array<{ key: string; label: string; episodes: any[]; baseItem: any }>
  >([]);
  const lastProgressSentRef = useRef<number>(0);

  const extractNextSeries = (srcItem: any): any | null => {
    try {
      const rel = srcItem?.related || srcItem?.nextSeries || null;
      if (Array.isArray(rel)) {
        return rel.find((r: any) => String(r?.relation || '').toLowerCase() === 'sequel') || null;
      }
      if (rel && typeof rel === 'object') return rel;
    } catch {
      return null;
    }
    return null;
  };

  const basenameFromPath = (p: string) => {
    try {
      const parts = String(p || '').split(/[\\/]/).filter(Boolean);
      return parts.length ? parts[parts.length - 1] : String(p || '');
    } catch (e) {
      return String(p || '');
    }
  };

  const guessEpisodeNumber = (fp: string): number | null => {
    try {
      const s = String(fp || '');
      const m = s.match(/\bS(\d{1,2})E(\d{1,3})\b/i);
      if (m) return Number(m[2]);
    } catch {}
    try {
      const bn = (String(fp || '').split(/[\\/]/).pop() || '').trim();
      const m = bn.match(/^(\d{1,3})\b/);
      if (m && m[1]) return Number(m[1]);
    } catch {}
    return null;
  };

  const buildEpisodesFromMedia = (media: any) => {
    const files = Array.isArray(media?.files) ? media.files : [];

    const sorted = [...files].sort((a, b) => {
      const ae = guessEpisodeNumber(a?.path || '') ?? a?.index ?? a?.file_index ?? 0;
      const be = guessEpisodeNumber(b?.path || '') ?? b?.index ?? b?.file_index ?? 0;
      return ae - be;
    });

    return sorted.map((f, i) => {
      const epNo = guessEpisodeNumber(f?.path || '') ?? f?.index ?? f?.file_index ?? i + 1;
      return { ...f, index: epNo, file_index: epNo, episodeTitle: f?.episodeTitle || undefined };
    });
  };

  const buildPlayerSeasons = async (base: any) => {
    if (!base) return [];
    const playlistSections = (() => {
      const raw = base?.seasonPlaylists;
      if (!raw || typeof raw !== 'object') return null;
      const parsed = Object.entries(raw)
        .map(([k, v]) => ({
          season: Number(k),
          episodes: Array.isArray(v) ? v : [],
        }))
        .filter((entry) => entry.episodes.length > 0);
      if (!parsed.length) return null;
      parsed.sort((a, b) => {
        if (Number.isFinite(a.season) && Number.isFinite(b.season)) return a.season - b.season;
        if (Number.isFinite(a.season)) return -1;
        if (Number.isFinite(b.season)) return 1;
        return 0;
      });
      return parsed.map((entry, idx) => {
        const seasonNumber = Number.isFinite(entry.season) ? entry.season : undefined;
        const normalizedEpisodes = entry.episodes.map((f: any, i: number) => {
          const epNo = guessEpisodeNumber(f?.path || '') ?? f?.index ?? f?.file_index ?? i + 1;
          return {
            ...f,
            index: epNo,
            file_index: epNo,
            episodeTitle: f?.episodeTitle || f?.title || undefined,
            seasonNumber: seasonNumber ?? f?.seasonNumber,
          };
        });
        const key = Number.isFinite(entry.season) ? `s${entry.season}` : `s-${idx + 1}`;
        const label = seasonNumber ? `Temporada ${seasonNumber}` : (base?.title || 'Temporada');
        return { key, label, episodes: normalizedEpisodes, baseItem: { ...(base as any), selectedSeason: seasonNumber ?? base?.selectedSeason } };
      });
    })();
    if (playlistSections && playlistSections.length) return playlistSections;

    const rel = Array.isArray(base.related) ? base.related : [];
    const prequels = rel.filter((r: any) => String(r?.relation || '').toLowerCase() === 'prequel');
    const sequels = rel.filter((r: any) => String(r?.relation || '').toLowerCase() === 'sequel');

    const sections: Array<{ key: string; label: string; episodes: any[]; baseItem: any }> = [];

    const episodesFromDb = async (media: any) => {
      try {
        const id = Number(media?.id);
        if (!Number.isFinite(id)) return buildEpisodesFromMedia(media);
        const res = await MediaService.getEpisodeSeasons(id, { includeRelated: false });
        const seasons = Array.isArray(res?.seasons) ? res.seasons : [];
        const targetSeason = Number(media?.selectedSeason ?? media?.seasonNumber ?? media?.season_number ?? NaN);
        const mediaPath = media?.path ? String(media.path) : '';
        const self =
          seasons.find((s: any) => Number(s?.media_item_id ?? s?.mediaItemId) === id) ||
          (Number.isFinite(targetSeason)
            ? seasons.find((s: any) => Number(s?.season_number ?? s?.seasonNumber ?? NaN) === targetSeason)
            : null) ||
          (mediaPath
            ? seasons.find(
              (s: any) =>
                Array.isArray(s?.episodes) &&
                s.episodes.some((e: any) => String(e?.file?.path || '') === mediaPath),
            )
            : null) ||
          seasons[0] ||
          null;
        const eps = self && Array.isArray(self.episodes) ? self.episodes : [];
        if (!eps.length) return buildEpisodesFromMedia(media);
        return eps.map((e: any) => {
          const epNo = Number(e?.episode_number ?? e?.episodeNumber ?? 0);
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
            seasonNumber: Number(self?.season_number ?? self?.seasonNumber ?? undefined),
          };
        });
      } catch {
        return buildEpisodesFromMedia(media);
      }
    };

    const addSection = async (media: any, label: string) => {
      const episodes = await episodesFromDb(media);
      const rawId = Number(media?.id);
      const baseKey = Number.isFinite(rawId) ? `m${rawId}` : `m-${sections.length + 1}`;
      const key = sections.some((s) => s.key === baseKey) ? `${baseKey}-${sections.length + 1}` : baseKey;
      sections.push({ key, label, episodes, baseItem: media });
    };

    for (const r of prequels) {
      try {
        const d = await MediaService.getById(Number(r.id));
        await addSection(d, r.title || d.title || 'Precuela');
      } catch {}
    }

    const primaryLabel = base.selectedSeason ? `Temporada ${base.selectedSeason}` : base.title || 'Temporada';
    await addSection(base, primaryLabel);

    for (const r of sequels) {
      try {
        const d = await MediaService.getById(Number(r.id));
        await addSection(d, r.title || d.title || 'Secuela');
      } catch {}
    }

    return sections;
  };

  const resolveResume = useCallback(
    async (itemId: number, files: any[], fallbackPath: string | null | undefined) => {
      try {
        const res = await MediaService.getPlaybackProgress(itemId, currentUser?.id);
        const pos = Number(res?.position ?? 0);
        const dur = Number(res?.duration ?? 0);
        const ratio = dur > 0 ? pos / dur : 0;
        if (!Number.isFinite(pos) || !Number.isFinite(dur) || dur <= 0) {
          return { startTime: 0, path: fallbackPath || null };
        }
        if (ratio >= 0.95 || pos < 5) {
          return { startTime: 0, path: fallbackPath || null };
        }
        const savedPath = res?.path ? String(res.path) : null;
        let nextPath = fallbackPath || null;
        if (savedPath && files && files.length) {
          const hit = files.find((f) => String(f?.path || '') === savedPath);
          if (hit) nextPath = savedPath;
        }
        return { startTime: pos, path: nextPath };
      } catch {
        return { startTime: 0, path: fallbackPath || null };
      }
    },
    [currentUser?.id],
  );

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      if (!playingMedia) {
        setPlayerSeasons([]);
        return;
      }
      const seasons = await buildPlayerSeasons(playingMedia);
      if (!cancelled) setPlayerSeasons(seasons);
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [playingMedia?.id, playingMedia?.path, lang]);

  useEffect(() => {
    lastProgressSentRef.current = 0;
  }, [playingMedia?.id, playingMedia?.path]);

  const handlePlayById = useCallback(
    async (id: number) => {
      try {
        const d = await MediaService.getById(id);
        const files = Array.isArray((d as any).files) ? (d as any).files : [];
        let path = (d as any).path;
        if (!path && files.length) path = files[0]?.path;
        const resume = await resolveResume(id, files, path);
        if (resume.path) path = resume.path;
        const nextSeries = extractNextSeries(d);
        const playItem: any = { ...(d as any), path, files, nextSeries };
        onCloseDetail?.();
        setPlayingStartTime(resume.startTime || 0);
        setPlayingMedia(playItem);
        recordPlay(id);
        try {
          await MediaService.recordPlay(id, currentUser?.id);
        } catch {}
        if (path) {
          setPlayingSrc(MediaService.getStreamUrlFromPath(path));
        } else {
          setPlayingSrc(null);
        }
      } catch (e) {
        console.error('Failed to load media by id', e);
      }
    },
    [currentUser?.id, onCloseDetail, recordPlay],
  );

  const handlePlay = useCallback(
    (item: MediaItem) => {
      (async () => {
        onCloseDetail?.();
        const anyItem: any = item as any;
        if (!anyItem.path && anyItem.id) {
          await handlePlayById(Number(anyItem.id));
          return;
        }
        if (anyItem.seasonPlaylists && anyItem.selectedSeason) {
          const list = anyItem.seasonPlaylists[anyItem.selectedSeason] || [];
          if (Array.isArray(list) && list.length) {
            anyItem.files = list;
            anyItem.path = anyItem.path || list[0]?.path;
          }
        }
        let resumeStart = 0;
        if (anyItem?.id) {
          try {
            const files = Array.isArray(anyItem?.files) ? anyItem.files : [];
            const resume = await resolveResume(Number(anyItem.id), files, anyItem.path);
            resumeStart = resume.startTime || 0;
            if (resume.path) anyItem.path = resume.path;
          } catch {}
        }
        const nextSeries = extractNextSeries(anyItem);
        setPlayingStartTime(resumeStart || 0);
        setPlayingMedia({ ...(anyItem as any), nextSeries });
        recordPlay(anyItem?.id ?? null);
        try {
          if (anyItem?.id) await MediaService.recordPlay(Number(anyItem.id), currentUser?.id);
        } catch {}
        try {
          const src = MediaService.getStreamUrlFromPath(anyItem.path);
          setPlayingSrc(src);
        } catch (e) {
          console.error('Failed to get stream url', e);
          setPlayingSrc(null);
        }
      })();
    },
    [currentUser?.id, handlePlayById, onCloseDetail, recordPlay],
  );

  const playingFiles: any[] = useMemo(() => {
    if (!playingMedia) return [];
    const fs = (playingMedia as any).files;
    return Array.isArray(fs) ? fs : [];
  }, [playingMedia]);

  const playingFileIndex = useMemo(() => {
    if (!playingMedia) return -1;
    if (!playingFiles.length) return -1;
    const curPath = playingMedia.path;
    const idx = playingFiles.findIndex((f: any) => f && f.path && f.path === curPath);
    return idx >= 0 ? idx : 0;
  }, [playingFiles, playingMedia]);

  const nowPlayingLabel = useMemo(() => {
    if (!playingMedia) return '';
    if (!playingFiles.length) return basenameFromPath(playingMedia.path);
    const f = playingFiles[Math.max(0, playingFileIndex)] || null;
    const name =
      (f && (f.episodeTitle || f.title || f.name || f.filename || (f.path ? basenameFromPath(f.path) : ''))) ||
      basenameFromPath(playingMedia.path);
    const ep = (f && (typeof f.index === 'number' ? f.index : (typeof f.file_index === 'number' ? f.file_index : undefined))) as
      | number
      | undefined;
    const season = f && typeof f.seasonNumber === 'number' && Number.isFinite(f.seasonNumber) ? f.seasonNumber : undefined;
    if (typeof ep === 'number' && Number.isFinite(ep)) {
      if (typeof season === 'number') return `Temporada ${season} - Episodio ${ep}: ${name}`;
      return `Episodio ${ep}: ${name}`;
    }
    return name;
  }, [playingMedia, playingFiles, playingFileIndex]);

  const playFromPlaylistIndex = useCallback(
    (nextIdx: number) => {
      if (!playingMedia) return;
      const f = playingFiles[nextIdx];
      const nextPath = f && f.path ? String(f.path) : '';
      if (!nextPath) return;
      try {
        const src = MediaService.getStreamUrlFromPath(nextPath);
        setPlayingSrc(src);
      } catch (e) {
        console.error('Failed to build stream url', e);
      }
      setPlayingStartTime(0);
      setPlayingMedia({ ...playingMedia, path: nextPath });
    },
    [playingFiles, playingMedia],
  );

  const handleNextPlayingItem = useCallback(() => {
    if (!playingMedia) return;
    if (!playingFiles.length) return;
    const next = Math.min(playingFiles.length - 1, Math.max(0, playingFileIndex) + 1);
    if (next === playingFileIndex) return;
    playFromPlaylistIndex(next);
  }, [playFromPlaylistIndex, playingFileIndex, playingFiles.length, playingMedia]);

  const handlePrevPlayingItem = useCallback(() => {
    if (!playingMedia) return;
    if (!playingFiles.length) return;
    const prev = Math.max(0, Math.max(0, playingFileIndex) - 1);
    if (prev === playingFileIndex) return;
    playFromPlaylistIndex(prev);
  }, [playFromPlaylistIndex, playingFileIndex, playingFiles.length, playingMedia]);

  const handleContinueNextSeries = useCallback(
    async (nextId: number) => {
      if (!Number.isFinite(nextId)) return;
      await handlePlayById(nextId);
    },
    [handlePlayById],
  );

  const handleSelectEpisode = useCallback(
    (seasonKey: string, ep: any) => {
      const section = playerSeasons.find((s) => s.key === seasonKey);
      if (!section || !ep?.path) return;
      const nextPath = String(ep.path);
      const nextFiles = section.episodes || [];
      const baseItem = section.baseItem || playingMedia;
      setPlayingStartTime(0);
      setPlayingMedia({ ...(baseItem as any), path: nextPath, files: nextFiles });
      try {
        const src = MediaService.getStreamUrlFromPath(nextPath);
        setPlayingSrc(src);
      } catch {}
    },
    [playerSeasons, playingMedia],
  );

  const closePlayer = useCallback(() => {
    setPlayingMedia(null);
    setPlayingSrc(null);
    setPlayingStartTime(0);
  }, []);

  const hasPrevItem = playingFiles.length > 1 && playingFileIndex > 0;
  const hasNextItem = playingFiles.length > 1 && playingFileIndex >= 0 && playingFileIndex < playingFiles.length - 1;

  const handleProgressUpdate = useCallback(
    (payload: { current: number; duration: number; progress?: number; force?: boolean; ended?: boolean; path?: string }) => {
      if (!playingMedia?.id) return;
      const now = Date.now();
      const force = !!payload.force || !!payload.ended;
      if (!force && now - lastProgressSentRef.current < 5000) return;
      lastProgressSentRef.current = now;
      const position = Math.max(0, Number(payload.current || 0));
      const duration = Math.max(0, Number(payload.duration || 0));
      // The native player walks the season playlist on its own, so it reports the
      // file it is actually playing instead of trusting the item the UI selected.
      const reportedPath = payload.path ? String(payload.path) : null;
      const path = reportedPath || ((playingMedia as any)?.path ? String((playingMedia as any).path) : null);
      MediaService.savePlaybackProgress(Number(playingMedia.id), { position, duration, path }, currentUser?.id).catch(() => {});
    },
    [currentUser?.id, playingMedia],
  );

  return {
    playingMedia,
    playingSrc,
    playerSeasons,
    nowPlayingLabel,
    playingStartTime,
    handlePlay,
    handlePlayById,
    handleNextPlayingItem,
    handlePrevPlayingItem,
    handleContinueNextSeries,
    handleSelectEpisode,
    handleProgressUpdate,
    closePlayer,
    hasPrevItem,
    hasNextItem,
  };
};
