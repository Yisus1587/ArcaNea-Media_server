import { useCallback, useEffect, useMemo, useState } from 'react';
import { AppStage, MediaItem, UserProfile, ViewState } from '../types';
import { MediaService } from '../services/api';

interface RecommendationParams {
  appStage: AppStage;
  currentUser: UserProfile | null;
  currentView: ViewState;
  searchQuery: string;
  baseItems: MediaItem[];
}

export const useRecommendations = ({
  appStage,
  currentUser,
  currentView,
  searchQuery,
  baseItems,
}: RecommendationParams) => {
  const playHistoryKey = useMemo(
    () => `arcanea-play-history:${currentUser?.id || 'default'}`,
    [currentUser?.id],
  );
  const [playHistoryTick, setPlayHistoryTick] = useState(0);
  const [recommendedForYou, setRecommendedForYou] = useState<MediaItem[]>([]);
  const [inProgressItems, setInProgressItems] = useState<MediaItem[]>([]);

  const loadPlayHistory = useCallback((): Array<{ id: string; ts: number }> => {
    try {
      const raw = localStorage.getItem(playHistoryKey);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter((e) => e && e.id && e.ts) : [];
    } catch {
      return [];
    }
  }, [playHistoryKey]);

  const recordPlay = useCallback(
    (id: string | number | null | undefined) => {
      if (!id) return;
      try {
        const list = loadPlayHistory();
        list.unshift({ id: String(id), ts: Date.now() });
        const trimmed = list.slice(0, 200);
        localStorage.setItem(playHistoryKey, JSON.stringify(trimmed));
        setPlayHistoryTick((t) => t + 1);
      } catch {
        // ignore
      }
    },
    [loadPlayHistory, playHistoryKey],
  );

  const playHistory = useMemo(() => loadPlayHistory(), [loadPlayHistory, playHistoryTick]);

  const mediaById = useMemo(() => {
    const map = new Map<string, MediaItem>();
    for (const it of baseItems) map.set(String(it.id), it);
    return map;
  }, [baseItems]);

  const inProgressFallback = useMemo(() => {
    const seen = new Set<string>();
    const out: MediaItem[] = [];
    const sorted = [...playHistory].sort((a, b) => b.ts - a.ts);
    for (const ev of sorted) {
      const id = String(ev.id);
      if (seen.has(id)) continue;
      const it = mediaById.get(id);
      if (it) {
        out.push(it);
        seen.add(id);
      }
      if (out.length >= 8) break;
    }
    return out;
  }, [playHistory, mediaById]);

  const trendingItems = useMemo(() => {
    const cutoff = Date.now() - 1000 * 60 * 60 * 24 * 30;
    const counts = new Map<string, number>();
    for (const ev of playHistory) {
      if (ev.ts < cutoff) continue;
      const id = String(ev.id);
      counts.set(id, (counts.get(id) || 0) + 1);
    }
    const scored = [...counts.entries()].map(([id, count]) => ({ id, count }));
    scored.sort((a, b) => b.count - a.count);
    const out: MediaItem[] = [];
    for (const s of scored) {
      const it = mediaById.get(String(s.id));
      if (it) out.push(it);
      if (out.length >= 8) break;
    }
    return out;
  }, [playHistory, mediaById]);

  const recommendedItems = useMemo(() => {
    const now = Date.now();
    const scored = baseItems.map((it) => {
      const rating = Number(it.rating || 0);
      const ratingScore = rating > 10 ? rating : rating * 10;
      const addedTs = Date.parse(String(it.addedAt || '')) || 0;
      const ageDays = addedTs ? (now - addedTs) / (1000 * 60 * 60 * 24) : 9999;
      const recencyScore = Math.max(0, 30 - ageDays);
      return { it, score: ratingScore + recencyScore };
    });
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, 10).map((s) => s.it);
  }, [baseItems]);

  useEffect(() => {
    if (appStage !== 'app') return;
    if (!currentUser) return;
    if (currentView !== 'home' || searchQuery) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await MediaService.getRecommendations(12, currentUser?.id);
        if (cancelled) return;
        const raw = Array.isArray(res?.items) ? res.items : [];
        const items = raw.map((it: any) => ({
          id: String(it.id),
          title: it.title_localized || it.title || '',
          type: it.media_type || it.type || 'movie',
          year: it.release_year || it.year,
          rating: it.rating,
          posterPath: it.poster_url || it.posterPath || it.poster_path,
          thumbnailUrl: it.poster_url || it.thumbnailUrl,
          backdropUrl: it.backdrop_url || it.backdrop_path || it.backdropUrl,
          addedAt: it.created_at || new Date().toISOString(),
          rawMetadata: it,
        }));
        setRecommendedForYou(items as any);
      } catch {
        // ignore
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [appStage, currentUser?.id, currentView, searchQuery, playHistoryTick]);

  useEffect(() => {
    if (appStage !== 'app') return;
    if (!currentUser) return;
    if (currentView !== 'home' || searchQuery) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await MediaService.getInProgress(8, currentUser?.id);
        if (cancelled) return;
        const raw = Array.isArray(res?.items) ? res.items : [];
        if (!raw.length) {
          setInProgressItems(inProgressFallback);
          return;
        }
        const items = raw.map((it: any) => ({
          id: String(it.id),
          title: it.title_localized || it.title || '',
          type: it.media_type || it.type || 'movie',
          year: it.release_year || it.year,
          rating: it.rating,
          posterPath: it.poster_url || it.posterPath || it.poster_path,
          thumbnailUrl: it.poster_url || it.thumbnailUrl,
          backdropUrl: it.backdrop_url || it.backdrop_path || it.backdropUrl,
          addedAt: it.created_at || new Date().toISOString(),
          rawMetadata: it,
          watchProgress: typeof it.watch_progress === 'number' ? Math.round(it.watch_progress) : undefined,
        }));
        setInProgressItems(items as any);
      } catch {
        if (!cancelled) setInProgressItems(inProgressFallback);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [appStage, currentUser?.id, currentView, searchQuery, playHistoryTick, inProgressFallback]);

  return {
    recordPlay,
    inProgressItems,
    trendingItems,
    recommendedItems,
    recommendedForYou,
  };
};
