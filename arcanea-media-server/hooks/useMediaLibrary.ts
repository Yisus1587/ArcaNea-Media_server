import { useCallback, useEffect, useRef, useState } from 'react';
import { AppStage, MediaItem, UserProfile, ViewState } from '../types';
import { MediaService } from '../services/api';
import { normalizeSearchText, fuzzyScore } from '../utils/search';

interface UseMediaLibraryParams {
  appStage: AppStage;
  currentUser: UserProfile | null;
  currentView: ViewState;
  searchQuery: string;
  lang: string;
  setCurrentView: (view: ViewState) => void;
  selectedMediaId?: string | number | null;
  onSelectedMediaUpdate?: (item: MediaItem) => void;
}

export const useMediaLibrary = ({
  appStage,
  currentUser,
  currentView,
  searchQuery,
  lang,
  setCurrentView,
  selectedMediaId,
  onSelectedMediaUpdate,
}: UseMediaLibraryParams) => {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const mediaCacheRef = useRef(new Map<string, { items: MediaItem[]; total: number; pagesLoaded: number }>());
  const mediaPageRef = useRef(1);
  const mediaItemsRef = useRef<MediaItem[]>([]);
  const loadingNextPageRef = useRef(false);
  const fetchSeqRef = useRef(0);
  const prefetchingRef = useRef(false);

  const [mediaItems, setMediaItems] = useState<MediaItem[]>([]);
  const [userListItems, setUserListItems] = useState<MediaItem[]>([]);
  const [userListLoading, setUserListLoading] = useState(false);
  const [mediaTotal, setMediaTotal] = useState(0);
  const [mediaPage, setMediaPage] = useState(1);
  const [hasMoreMedia, setHasMoreMedia] = useState(false);
  const [loadingMoreMedia, setLoadingMoreMedia] = useState(false);
  const [suggestedItems, setSuggestedItems] = useState<MediaItem[]>([]);
  const [localSearchActive, setLocalSearchActive] = useState(false);
  const [localSearchResults, setLocalSearchResults] = useState<MediaItem[]>([]);
  const [profileLoadingHint, setProfileLoadingHint] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const _computeMediaTypes = useCallback(
    (profile: UserProfile | null, view: ViewState, q: string): string[] | undefined => {
      const qq = (q || '').trim();
      if (qq) return undefined;
      if (profile?.isKid) return ['anime'];
      if (view === 'list') return undefined;
      if (view === 'movies') return ['movie'];
      if (view === 'tv') return ['series', 'tv', 'anime'];
      return undefined;
    },
    [],
  );

  const _isKidSafeItem = useCallback((it: MediaItem): boolean => {
    try {
      const md: any = (it as any).rawMetadata || null;
      const raw: any = md && md.raw ? md.raw : md;
      if (raw && raw.adult === true) return false;
      const rating = String(md?.rating || raw?.rating || '').toLowerCase();
      if (rating.includes('rx') || rating.includes('r - 17') || rating.includes('r+')) return false;
      const genres = Array.isArray(it.genre) ? it.genre : [];
      const gLower = genres.map((g) => String(g || '').toLowerCase());
      if (gLower.some((g) => g.includes('hentai') || g.includes('ecchi') || g.includes('erot'))) return false;
      const extra = ([] as string[]).concat(md?.explicit_genres || [], md?.themes || [], md?.demographics || []);
      const extraLower = extra.map((x) => String(x || '').toLowerCase());
      if (extraLower.some((g) => g.includes('hentai') || g.includes('ecchi') || g.includes('erot'))) return false;
      return true;
    } catch {
      return true;
    }
  }, []);

  const invalidateMediaCache = useCallback(() => {
    try {
      mediaCacheRef.current.clear();
    } catch {
      // ignore
    }
  }, []);

  const fetchMedia = useCallback(
    async (opts?: { reset?: boolean; userProfile?: UserProfile; silent?: boolean }) => {
      const profile = opts?.userProfile || currentUser;
      if (!profile) return;

      const pageSize = 50;
      const reset = !!opts?.reset;
      const silent = !!opts?.silent;
      if (!reset && loadingNextPageRef.current) return;
      loadingNextPageRef.current = true;
      const fetchSeq = ++fetchSeqRef.current;
      const types = _computeMediaTypes(profile, currentView, searchQuery);
      const cacheKey = `${profile.isKid ? 'kid' : 'std'}|${currentView}|${(types || []).join(',')}|q:${(searchQuery || '')
        .trim()
        .toLowerCase()}`;
      const compactHome = currentView === 'home' && !(searchQuery || '').trim();

      if (reset) {
        if (!silent) setLoading(true);
        setMediaPage(1);
        if (!silent) {
          setMediaTotal(0);
          setHasMoreMedia(false);
        }
      } else {
        setLoadingMoreMedia(true);
      }

      try {
        const cached = mediaCacheRef.current.get(cacheKey);
        const targetPage = reset ? 1 : (mediaPageRef.current || 1) + 1;
        if (cached && cached.pagesLoaded >= targetPage) {
          const slice = cached.items.slice(0, targetPage * pageSize);
          const nextItems = profile.isKid ? slice.filter(_isKidSafeItem) : slice;
          if (fetchSeq !== fetchSeqRef.current) return;
          setMediaItems(nextItems);
          setMediaTotal(cached.total);
          setMediaPage(targetPage);
          const cachedTotalVal = typeof cached.total === 'number' && cached.total > 0 ? cached.total : null;
          const cachedHasMore = cachedTotalVal ? targetPage * pageSize < cachedTotalVal : slice.length === targetPage * pageSize;
          setHasMoreMedia(cachedHasMore);
          setError(null);
          return;
        }

        const data = await MediaService.getAll(targetPage, pageSize, (searchQuery || '').trim() || undefined, types, !!profile.isKid);
        const baseIncoming = Array.isArray(data.items) ? data.items : [];
        const incoming = profile.isKid ? baseIncoming.filter(_isKidSafeItem) : baseIncoming;
        const existing = reset ? [] : mediaItemsRef.current || [];
        const next = reset
          ? incoming
          : (() => {
              const seen = new Set(existing.map((i) => i.id));
              const out = [...existing];
              for (const it of incoming) {
                if (seen.has(it.id)) continue;
                seen.add(it.id);
                out.push(it);
              }
              return out;
            })();

        if (fetchSeq !== fetchSeqRef.current) return;
        setMediaItems(next);
        if (reset && searchQuery) {
          setLocalSearchActive(false);
          setLocalSearchResults([]);
        }
        if (reset) setProfileLoadingHint(false);
        if (reset && searchQuery) {
          const qn = normalizeSearchText(searchQuery);
          if (qn && incoming.length === 0) {
            const baseTypes = _computeMediaTypes(profile, currentView, '');
            const baseKey = `${profile.isKid ? 'kid' : 'std'}|${currentView}|${(baseTypes || []).join(',')}|q:`;
            const baseCached = mediaCacheRef.current.get(baseKey);
            const baseCandidates =
              baseCached?.items && baseCached.items.length ? baseCached.items : mediaItemsRef.current || [];
            const filteredCandidates = profile.isKid ? baseCandidates.filter(_isKidSafeItem) : baseCandidates;
            const scored = filteredCandidates
              .map((it) => {
                const tn = normalizeSearchText(String(it?.title || ''));
                const score = fuzzyScore(qn, tn);
                return { it, score };
              })
              .filter((s) => s.score >= 0.45)
              .sort((a, b) => b.score - a.score)
              .slice(0, 12)
              .map((s) => s.it);
            setSuggestedItems(scored);
          } else {
            setSuggestedItems([]);
          }
        } else {
          setSuggestedItems([]);
        }
        setMediaTotal(data.total || next.length);
        setMediaPage(targetPage);
        const totalVal = typeof data.total === 'number' && data.total > 0 ? data.total : null;
        const fetchedRaw = (targetPage - 1) * pageSize + baseIncoming.length;
        const hasMore = totalVal ? fetchedRaw < totalVal : baseIncoming.length === pageSize;
        setHasMoreMedia(hasMore);
        setError(null);

        try {
          const prev = mediaCacheRef.current.get(cacheKey);
          const mergedItems = reset ? baseIncoming : [...(prev?.items || []), ...baseIncoming];
          const seen = new Set<string>();
          const deduped: MediaItem[] = [];
          for (const it of mergedItems) {
            if (!it || !it.id) continue;
            if (seen.has(it.id)) continue;
            seen.add(it.id);
            deduped.push(it);
          }
          mediaCacheRef.current.set(cacheKey, {
            items: deduped,
            total: data.total || deduped.length,
            pagesLoaded: Math.max(prev?.pagesLoaded || 0, targetPage),
          });
        } catch {
          // ignore cache failures
        }

        if (reset) {
          const total = typeof data.total === 'number' ? data.total : 0;
          const shouldPrefetch = total > pageSize && !prefetchingRef.current && !compactHome;
          if (shouldPrefetch) {
            prefetchingRef.current = true;
            void (async () => {
              try {
                const nextPage = targetPage + 1;
                const res = await MediaService.getAll(nextPage, pageSize, (searchQuery || '').trim() || undefined, types, !!profile.isKid);
                const list = Array.isArray(res.items) ? res.items : [];
                const prev = mediaCacheRef.current.get(cacheKey);
                if (prev && list.length) {
                  const merged = [...prev.items, ...list];
                  const seen = new Set<string>();
                  const deduped: MediaItem[] = [];
                  for (const it of merged) {
                    if (!it || !it.id) continue;
                    if (seen.has(it.id)) continue;
                    seen.add(it.id);
                    deduped.push(it);
                  }
                  mediaCacheRef.current.set(cacheKey, {
                    items: deduped,
                    total: res.total || deduped.length,
                    pagesLoaded: Math.max(prev.pagesLoaded, nextPage),
                  });
                }
              } catch {
                // ignore
              } finally {
                prefetchingRef.current = false;
              }
            })();
          }
        }
      } catch (e) {
        console.warn('Failed to fetch media', e);
        setError('No se pudo conectar con el backend de medios');
      } finally {
        loadingNextPageRef.current = false;
        setLoading(false);
        setLoadingMoreMedia(false);
      }
    },
    [_computeMediaTypes, _isKidSafeItem, appStage, currentUser, currentView, searchQuery],
  );

  const refreshMediaItem = useCallback(async (id: string) => {
    try {
      const d = await MediaService.getById(Number(id));
      if (!d) return;
      const bustCache = (url?: string) => {
        if (!url) return url;
        const sep = url.includes('?') ? '&' : '?';
        return `${url}${sep}_t=${Date.now()}`;
      };
      try {
        (d as any).posterPath = bustCache((d as any).posterPath || (d as any).poster_url);
        (d as any).thumbnailUrl = bustCache((d as any).thumbnailUrl || (d as any).poster_url);
        (d as any).backdropUrl = bustCache((d as any).backdropUrl || (d as any).backdrop_path);
      } catch {
        // ignore
      }
      setMediaItems((prev) => prev.map((it) => (String(it.id) === String(id) ? (d as any) : it)));
      setUserListItems((prev) => prev.map((it) => (String(it.id) === String(id) ? (d as any) : it)));
      if (selectedMediaId && String(selectedMediaId) === String(id) && onSelectedMediaUpdate) {
        onSelectedMediaUpdate(d as any);
      }
      try {
        mediaCacheRef.current.forEach((value, key) => {
          if (!value?.items?.length) return;
          const updated = value.items.map((it) => (String(it.id) === String(id) ? (d as any) : it));
          mediaCacheRef.current.set(key, { ...value, items: updated });
        });
      } catch {
        // ignore cache updates
      }
    } catch {
      // ignore
    }
  }, [onSelectedMediaUpdate, selectedMediaId]);

  const refreshUserList = useCallback(async (profile: UserProfile | null) => {
    if (!profile?.id) return;
    setUserListLoading(true);
    try {
      const res = await MediaService.getUserList(profile.id);
      const ids = Array.isArray(res?.items) ? res.items : [];
      const promises = ids.map((id: any) => MediaService.getById(Number(id)));
      const settled = await Promise.allSettled(promises);
      const items = settled
        .filter((s) => s.status === 'fulfilled')
        .map((s: any) => s.value)
        .filter(Boolean);
      setUserListItems(items);
    } catch {
      setUserListItems([]);
    } finally {
      setUserListLoading(false);
    }
  }, []);

  const resetMediaState = useCallback(() => {
    setMediaItems([]);
    setMediaTotal(0);
    setHasMoreMedia(false);
    setMediaPage(1);
  }, []);

  // Keep refs in sync to avoid stale values in IntersectionObserver callbacks.
  useEffect(() => {
    mediaPageRef.current = mediaPage;
  }, [mediaPage]);
  useEffect(() => {
    mediaItemsRef.current = mediaItems;
  }, [mediaItems]);

  // Load first page when profile/view changes (except settings).
  useEffect(() => {
    if (appStage !== 'app') return;
    if (!currentUser) return;
    if (currentView === 'settings' || currentView === 'list') return;
    void fetchMedia({ reset: true, userProfile: currentUser });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appStage, currentUser?.id, currentView]);

  // Infinite scroll: when the sentinel enters view, load the next page.
  useEffect(() => {
    if (appStage !== 'app') return;
    if (!currentUser) return;
    if (currentView === 'settings' || currentView === 'list') return;
    if (currentView === 'home' && !(searchQuery || '').trim()) return;
    if (!hasMoreMedia) return;
    if (loading || loadingMoreMedia) return;
    const root = scrollContainerRef.current;
    const el = sentinelRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => {
        const first = entries && entries[0];
        if (!first || !first.isIntersecting) return;
        void fetchMedia({ reset: false, userProfile: currentUser });
      },
      { root: root || null, rootMargin: '800px 0px 800px 0px', threshold: 0.01 },
    );
    obs.observe(el);
    return () => obs.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appStage, currentUser?.id, currentView, searchQuery, hasMoreMedia, loadingMoreMedia, loading]);

  // Scroll fallback: ensure we keep paging even if the sentinel stays intersecting.
  useEffect(() => {
    if (appStage !== 'app') return;
    if (!currentUser) return;
    if (currentView === 'settings' || currentView === 'list') return;
    if (currentView === 'home' && !(searchQuery || '').trim()) return;
    const el = scrollContainerRef.current;
    if (!el) return;
    let ticking = false;
    const check = () => {
      if (!hasMoreMedia || loading || loadingMoreMedia) return;
      const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 800;
      if (nearBottom) {
        void fetchMedia({ reset: false, userProfile: currentUser });
      }
    };
    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        check();
      });
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    check();
    return () => el.removeEventListener('scroll', onScroll);
  }, [appStage, currentUser?.id, currentView, searchQuery, hasMoreMedia, loadingMoreMedia, loading, fetchMedia]);

  // Debounced search logic - GLOBAL SEARCH
  useEffect(() => {
    if (appStage !== 'app') return;

    if (searchQuery && currentView === 'settings') {
      setCurrentView('home');
      return;
    }

    if (currentView === 'settings' || currentView === 'list') return;

    let cancelled = false;
    const q = (searchQuery || '').trim();
    if (!q) {
      setLocalSearchActive(false);
      setLocalSearchResults([]);
    }

    // Single debounce for both: (1) remote search results, (2) quick suggestions.
    // This prevents 2x request bursts on every keystroke and keeps the UI responsive.
    const timeoutId = window.setTimeout(() => {
      if (cancelled) return;

      if (q.length >= 2) {
        (async () => {
          try {
            const profile = currentUser;
            if (profile) {
              const types = _computeMediaTypes(profile, currentView, '');
              const res = await MediaService.searchSuggest(q, 20, types);
              if (cancelled) return;
              const list = Array.isArray(res?.items) ? res.items : [];
              if (list.length) {
                setLocalSearchActive(true);
                setLocalSearchResults(list);
              } else {
                setLocalSearchActive(false);
                setLocalSearchResults([]);
              }
            }
          } catch {
            if (cancelled) return;
            setLocalSearchActive(false);
            setLocalSearchResults([]);
          }
        })();
      } else if (!q) {
        setLocalSearchActive(false);
        setLocalSearchResults([]);
      }

      fetchMedia({ reset: true });
    }, q ? 320 : 0);

    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
    };
  }, [searchQuery, appStage, currentView, currentUser?.id]);

  // When UI language changes, refetch the current page so titles/overview (TMDB i18n) re-map.
  useEffect(() => {
    if (appStage !== 'app') return;
    if (!currentUser) return;
    if (currentView === 'settings' || currentView === 'list') return;
    void fetchMedia({ reset: true, userProfile: currentUser });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lang]);

  return {
    scrollContainerRef,
    sentinelRef,
    mediaItems,
    userListItems,
    userListLoading,
    mediaTotal,
    mediaPage,
    hasMoreMedia,
    loadingMoreMedia,
    suggestedItems,
    localSearchActive,
    localSearchResults,
    profileLoadingHint,
    setProfileLoadingHint,
    loading,
    setLoading,
    error,
    fetchMedia,
    refreshMediaItem,
    refreshUserList,
    invalidateMediaCache,
    resetMediaState,
  };
};
