import React, { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowUpDown, Loader2, RefreshCw, Search, ServerCrash } from 'lucide-react';
import { MediaCard } from '../media/MediaCard';
import { FeaturedHero } from '../media/FeaturedHero';
import { LibraryOverview } from '../media/LibraryOverview';
import { InlineLoader } from '../ui/InlineLoader';
import { Button } from '../ui/Button';
import { Drive, MediaItem, MetadataConfig, ScanStatus, UserProfile, ViewState } from '../../types';
import { useI18n } from '../../i18n/i18n';
import { MediaService } from '../../services/api';

const SettingsView = lazy(() =>
  import('../settings/SettingsView').then((module) => ({ default: module.SettingsView })),
);

type SortOption = 'date' | 'title' | 'rating';

function _stripSeasonSuffix(title: string): string {
  try {
    const s = String(title || '').trim();
    if (!s) return s;
    // Common suffixes injected by scan/enrichment for seasons.
    return s
      .replace(/\s*-\s*(?:temporada|season|part|cour)\s*\d+\s*$/i, '')
      .replace(/\s*\(\s*(?:temporada|season|part|cour)\s*\d+\s*\)\s*$/i, '')
      .trim();
  } catch {
    return title;
  }
}

function _isSeasonLikeSeriesItem(it: MediaItem): boolean {
  if (!it) return false;
  if (it.type !== 'series' && it.type !== 'anime') return false;
  const sn = (it as any).seasonNumber;
  if (typeof sn !== 'number' || !Number.isFinite(sn) || sn <= 0) return false;
  const sid = (it as any).seriesId ?? (it as any).tmdbId;
  if (sid == null) return false;
  const s = String(sid).trim();
  return s.length > 0;
}

function _seriesKey(it: MediaItem): string | null {
  try {
    const sid = (it as any).seriesId ?? (it as any).tmdbId;
    if (sid == null) return null;
    const s = String(sid).trim();
    if (!s) return null;
    return `${it.type}:${s}`;
  } catch {
    return null;
  }
}

function _pickBetterSeriesCard(a: MediaItem, b: MediaItem): MediaItem {
  const snA = typeof a.seasonNumber === 'number' && Number.isFinite(a.seasonNumber) ? a.seasonNumber : 9999;
  const snB = typeof b.seasonNumber === 'number' && Number.isFinite(b.seasonNumber) ? b.seasonNumber : 9999;
  if (snB < snA) return b;
  if (snA < snB) return a;

  // Prefer identified + better artwork.
  const ia = (a as any).is_identified ? 1 : 0;
  const ib = (b as any).is_identified ? 1 : 0;
  if (ib !== ia) return ib > ia ? b : a;
  const pa = (typeof a.posterPath === 'string' && a.posterPath.trim()) ? 1 : 0;
  const pb = (typeof b.posterPath === 'string' && b.posterPath.trim()) ? 1 : 0;
  if (pb !== pa) return pb > pa ? b : a;
  const ba = (typeof a.backdropUrl === 'string' && a.backdropUrl.trim()) ? 1 : 0;
  const bb = (typeof b.backdropUrl === 'string' && b.backdropUrl.trim()) ? 1 : 0;
  if (bb !== ba) return bb > ba ? b : a;
  return a;
}

function dedupeSeriesForGrid(items: MediaItem[]): MediaItem[] {
  const out: MediaItem[] = [];
  const idxBySeriesKey = new Map<string, number>();

  for (const raw of items || []) {
    if (!_isSeasonLikeSeriesItem(raw)) {
      out.push(raw);
      continue;
    }
    const key = _seriesKey(raw);
    if (!key) {
      out.push(raw);
      continue;
    }
    const normalized: MediaItem = (() => {
      const stripped = _stripSeasonSuffix(raw.title);
      return stripped && stripped !== raw.title ? { ...raw, title: stripped } : raw;
    })();

    const existingIdx = idxBySeriesKey.get(key);
    if (existingIdx == null) {
      idxBySeriesKey.set(key, out.length);
      out.push(normalized);
      continue;
    }
    const existing = out[existingIdx];
    const better = _pickBetterSeriesCard(existing, normalized);
    if (better !== existing) out[existingIdx] = better;
  }

  return out;
}

function _normalizedGenres(it: MediaItem | null | undefined): string[] {
  try {
    const raw = Array.isArray(it?.genre) ? it?.genre : [];
    return raw
      .map((g) => String(g || '').trim())
      .filter(Boolean)
      .map((g) => g.toLowerCase());
  } catch {
    return [];
  }
}

function _nextHeroIndex(prev: number, total: number): number {
  if (!Number.isFinite(total) || total <= 1) return 0;
  const next = (prev + 1 + Math.floor(Math.random() * Math.max(1, total - 1))) % total;
  return next === prev ? (prev + 1) % total : next;
}

interface AppContentProps {
  currentView: ViewState;
  currentUser: UserProfile | null;
  drives: Drive[];
  libraryPaths: string[];
  looseMovieRoots: string[];
  rootTypes: Record<string, string>;
  scanStatus: ScanStatus;
  enrichment: any | null;
  migrationRunning: boolean;
  migrationError: string | null;
  metadataConfig: MetadataConfig;
  profiles: UserProfile[];
  error: string | null;
  loading: boolean;
  profileLoadingHint: boolean;
  searchQuery: string;
  lang: string;
  baseItems: MediaItem[];
  userListLoading: boolean;
  suggestedItems: MediaItem[];
  hasMoreMedia: boolean;
  loadingMoreMedia: boolean;
  mediaTotal: number;
  scrollContainerRef: React.RefObject<HTMLDivElement>;
  sentinelRef: React.RefObject<HTMLDivElement>;
  recommendedItems: MediaItem[];
  recommendedForYou: MediaItem[];
  inProgressItems: MediaItem[];
  trendingItems: MediaItem[];
  onChangeView: (view: ViewState) => void;
  onStartEnrich: () => void;
  onStartScan: () => void;
  onAddPath: (path: string, type?: string) => void;
  onRemovePath: (path: string) => void;
  onUpdateConfig: (cfg: MetadataConfig) => void;
  onToggleLooseMovieRoot: (path: string) => void;
  onSetRootType: (path: string, type: string) => void;
  onRefreshSettings: () => void;
  onAddProfile: (profile: Omit<UserProfile, 'id'>) => void;
  onUpdateProfile: (profile: UserProfile) => void;
  onDeleteProfile: (id: string) => void;
  onSelectMedia: (item: MediaItem) => void;
  onPlay: (item: MediaItem) => void;
  onRefreshMedia: () => void;
  onLoadMoreMedia: () => void;
}

export const AppContent: React.FC<AppContentProps> = ({
  currentView,
  currentUser,
  drives,
  libraryPaths,
  looseMovieRoots,
  rootTypes,
  scanStatus,
  enrichment,
  migrationRunning,
  migrationError,
  metadataConfig,
  profiles,
  error,
  loading,
  profileLoadingHint,
  searchQuery,
  baseItems,
  userListLoading,
  suggestedItems,
  hasMoreMedia,
  loadingMoreMedia,
  mediaTotal,
  scrollContainerRef,
  sentinelRef,
  recommendedItems,
  recommendedForYou,
  inProgressItems,
  trendingItems,
  onChangeView,
  onStartEnrich,
  onStartScan,
  onAddPath,
  onRemovePath,
  onUpdateConfig,
  onToggleLooseMovieRoot,
  onSetRootType,
  onRefreshSettings,
  onAddProfile,
  onUpdateProfile,
  onDeleteProfile,
  onSelectMedia,
  onPlay,
  onRefreshMedia,
  onLoadMoreMedia,
}) => {
  const { t } = useI18n();
  const [sortBy, setSortBy] = useState<SortOption>('date');
  const [gridCols, setGridCols] = useState(6);
  const [homeAiringPool, setHomeAiringPool] = useState<MediaItem[]>([]);
  const [featuredHeroIndex, setFeaturedHeroIndex] = useState(0);

  const computeGrid = useCallback(() => {
    try {
      const w = window.innerWidth;
      let cols = 6;
      if (w < 640) cols = 2;
      else if (w < 768) cols = 3;
      else if (w < 1024) cols = 4;
      else if (w < 1280) cols = 5;
      else cols = 6;

      const container = scrollContainerRef.current;
      const containerWidth = container ? container.clientWidth : window.innerWidth;
      const gap = w < 640 ? 12 : 24;
      const usable = Math.max(0, containerWidth - gap * (cols - 1));

      setGridCols(cols);
    } catch {
      // ignore
    }
  }, [scrollContainerRef]);

  useEffect(() => {
    computeGrid();
    const onResize = () => computeGrid();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [computeGrid]);

  const dedupedBaseItems = useMemo(() => dedupeSeriesForGrid(baseItems), [baseItems]);

  const sortedMediaItems = useMemo(() => {
    const items = [...dedupedBaseItems];
    switch (sortBy) {
      case 'title':
        return items.sort((a, b) => a.title.localeCompare(b.title));
      case 'rating':
        return items.sort((a, b) => (b.rating || 0) - (a.rating || 0));
      case 'date':
      default:
        return items.sort((a, b) => b.addedAt.localeCompare(a.addedAt));
    }
  }, [dedupedBaseItems, sortBy]);

  const scoreHomeRelevance = useCallback((it: MediaItem): number => {
    const ratingRaw = Number(it.rating || 0);
    const rating = Number.isFinite(ratingRaw) ? (ratingRaw > 10 ? ratingRaw / 10 : ratingRaw) : 0;
    const addedTs = Date.parse(String(it.addedAt || '')) || 0;
    const ageDays = addedTs ? (Date.now() - addedTs) / (1000 * 60 * 60 * 24) : 9999;
    const recency = Math.max(0, 40 - ageDays); // more weight to fresh titles
    const hasBackdrop = typeof it.backdropUrl === 'string' && !!it.backdropUrl.trim() ? 1 : 0;
    const hasPoster = typeof it.posterPath === 'string' && !!it.posterPath.trim() ? 1 : 0;
    const identified = (it as any).is_identified ? 1 : 0;
    const airingBoost = it.isAiring ? 1 : 0;
    return (rating * 6) + recency + (hasBackdrop * 8) + (hasPoster * 3) + (identified * 4) + (airingBoost * 5);
  }, []);

  const homeSectionCaps = useMemo(() => {
    if (gridCols <= 2) {
      return { recommended: 4, airingAnime: 4, airingSeries: 4, inProgress: 4, trending: 4, added: 12 };
    }
    if (gridCols <= 4) {
      return { recommended: 6, airingAnime: 6, airingSeries: 4, inProgress: 6, trending: 6, added: 18 };
    }
    return { recommended: 8, airingAnime: 8, airingSeries: 6, inProgress: 6, trending: 6, added: 24 };
  }, [gridCols]);

  const featuredSeedItem = useMemo(() => {
    if (dedupedBaseItems.length === 0) return null;
    return [...dedupedBaseItems].sort((a, b) => scoreHomeRelevance(b) - scoreHomeRelevance(a))[0] || null;
  }, [dedupedBaseItems, scoreHomeRelevance]);

  const heroSignals = useMemo(() => {
    const watched = dedupeSeriesForGrid(inProgressItems || []);
    const watchedIds = new Set(watched.map((it) => String(it.id)));
    const personalIds = new Set(dedupeSeriesForGrid(recommendedForYou || []).map((it) => String(it.id)));
    const recommendedIds = new Set(dedupeSeriesForGrid(recommendedItems || []).map((it) => String(it.id)));
    const trendingIds = new Set(dedupeSeriesForGrid(trendingItems || []).map((it) => String(it.id)));
    const genreWeights = new Map<string, number>();
    watched.forEach((it, idx) => {
      const weight = Math.max(2, 8 - idx);
      _normalizedGenres(it).forEach((genre) => {
        genreWeights.set(genre, (genreWeights.get(genre) || 0) + weight);
      });
    });
    return { watchedIds, personalIds, recommendedIds, trendingIds, genreWeights };
  }, [inProgressItems, recommendedForYou, recommendedItems, trendingItems]);

  const featuredHeroPool = useMemo(() => {
    const byId = new Map<string, MediaItem>();
    const pushItems = (items: MediaItem[] | undefined) => {
      for (const item of dedupeSeriesForGrid(items || [])) {
        const key = String(item.id);
        if (!byId.has(key)) byId.set(key, item);
      }
    };
    pushItems(inProgressItems);
    pushItems(recommendedForYou);
    pushItems(recommendedItems);
    pushItems(trendingItems);
    pushItems(dedupedBaseItems);

    const ranked = Array.from(byId.values()).sort((a, b) => {
      const scoreCandidate = (it: MediaItem) => {
        let score = scoreHomeRelevance(it);
        const id = String(it.id);
        if (heroSignals.watchedIds.has(id)) score += 30;
        if (heroSignals.personalIds.has(id)) score += 24;
        else if (heroSignals.recommendedIds.has(id)) score += 16;
        if (heroSignals.trendingIds.has(id)) score += 12;
        for (const genre of _normalizedGenres(it)) {
          score += heroSignals.genreWeights.get(genre) || 0;
        }
        if (!String(it.backdropUrl || '').trim()) score -= 8;
        return score;
      };
      return scoreCandidate(b) - scoreCandidate(a);
    });
    return ranked.slice(0, 7);
  }, [
    dedupedBaseItems,
    heroSignals,
    inProgressItems,
    recommendedForYou,
    recommendedItems,
    scoreHomeRelevance,
    trendingItems,
  ]);

  const featuredPoolKey = useMemo(
    () => featuredHeroPool.map((it) => String(it.id)).join('|'),
    [featuredHeroPool],
  );

  useEffect(() => {
    setFeaturedHeroIndex(0);
  }, [featuredPoolKey, currentView, searchQuery]);

  useEffect(() => {
    if (currentView !== 'home' || !!searchQuery || featuredHeroPool.length <= 1) return;
    const id = window.setInterval(() => {
      setFeaturedHeroIndex((prev) => _nextHeroIndex(prev, featuredHeroPool.length));
    }, 12000);
    return () => window.clearInterval(id);
  }, [currentView, searchQuery, featuredHeroPool.length]);

  const featuredItem = featuredHeroPool[featuredHeroIndex] || featuredSeedItem;
  const featuredHeroMeta = useMemo(() => {
    if (!featuredItem) return { eyebrow: t('featured'), insight: null as string | null };
    const itemId = String(featuredItem.id);
    const topGenre = _normalizedGenres(featuredItem).find((genre) => heroSignals.genreWeights.has(genre)) || null;
    if (heroSignals.watchedIds.has(itemId)) {
      return {
        eyebrow: t('home_in_progress_title'),
        insight: topGenre ? `Basado en lo que sigues viendo: ${topGenre}` : 'Basado en tu historial reciente',
      };
    }
    if (heroSignals.personalIds.has(itemId)) {
      return {
        eyebrow: t('home_recommended_title'),
        insight: topGenre ? `Afinado por genero: ${topGenre}` : 'Ajustado a tu perfil',
      };
    }
    if (topGenre) {
      return {
        eyebrow: t('featured'),
        insight: `Rotando por tus generos: ${topGenre}`,
      };
    }
    if (heroSignals.trendingIds.has(itemId)) {
      return {
        eyebrow: t('home_trending_title'),
        insight: 'Destacado por tendencia reciente',
      };
    }
    return { eyebrow: t('featured'), insight: 'Seleccion dinamica del catalogo' };
  }, [featuredItem, heroSignals, t]);

  const airingItems = useMemo(
    () =>
      dedupedBaseItems.filter(
        (it) =>
          !!it.isAiring &&
          (it.type === 'anime' || it.type === 'series'),
      ),
    [dedupedBaseItems],
  );

  useEffect(() => {
    let cancelled = false;
    if (currentView !== 'home' || !!searchQuery) {
      setHomeAiringPool([]);
      return;
    }
    (async () => {
      try {
        const first = await MediaService.getAll(1, 120, undefined, ['anime', 'series'], !!currentUser?.isKid);
        if (cancelled) return;
        let items: MediaItem[] = Array.isArray(first?.items) ? first.items : [];
        const total = Number(first?.total || items.length || 0);
        if (total > 120) {
          try {
            const second = await MediaService.getAll(2, 120, undefined, ['anime', 'series'], !!currentUser?.isKid);
            if (!cancelled && Array.isArray(second?.items) && second.items.length) {
              items = [...items, ...second.items];
            }
          } catch {
            // ignore page 2 fetch errors
          }
        }
        if (cancelled) return;
        const pooled = dedupeSeriesForGrid(items).filter(
          (it) => !!it.isAiring && (it.type === 'anime' || it.type === 'series'),
        );
        setHomeAiringPool(pooled);
      } catch {
        if (!cancelled) setHomeAiringPool([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [currentView, searchQuery, currentUser?.isKid]);

  const homeRecommendedItems = useMemo(() => {
    const src = recommendedForYou && recommendedForYou.length ? recommendedForYou : recommendedItems;
    return dedupeSeriesForGrid(src || []).slice(0, homeSectionCaps.recommended);
  }, [recommendedForYou, recommendedItems, homeSectionCaps.recommended]);

  const homeAiringAnimeItems = useMemo(
    () => {
      const source = homeAiringPool.length ? homeAiringPool : airingItems;
      const ranked = dedupeSeriesForGrid(source || [])
        .filter((it) => it.type === 'anime')
        .sort((a, b) => scoreHomeRelevance(b) - scoreHomeRelevance(a));
      return ranked.slice(0, homeSectionCaps.airingAnime);
    },
    [homeAiringPool, airingItems, homeSectionCaps.airingAnime, scoreHomeRelevance],
  );

  const homeAiringSeriesItems = useMemo(
    () => {
      const source = homeAiringPool.length ? homeAiringPool : airingItems;
      const ranked = dedupeSeriesForGrid(source || [])
        .filter((it) => it.type === 'series')
        .sort((a, b) => scoreHomeRelevance(b) - scoreHomeRelevance(a));
      return ranked.slice(0, homeSectionCaps.airingSeries);
    },
    [homeAiringPool, airingItems, homeSectionCaps.airingSeries, scoreHomeRelevance],
  );

  const homeInProgressItems = useMemo(
    () => dedupeSeriesForGrid(inProgressItems || []).slice(0, homeSectionCaps.inProgress),
    [inProgressItems, homeSectionCaps.inProgress],
  );

  const homeTrendingItems = useMemo(
    () => dedupeSeriesForGrid(trendingItems || []).slice(0, homeSectionCaps.trending),
    [trendingItems, homeSectionCaps.trending],
  );

  const homeAddedItems = useMemo(() => {
    const ranked = [...dedupedBaseItems].sort((a, b) => {
      const diff = scoreHomeRelevance(b) - scoreHomeRelevance(a);
      if (Math.abs(diff) > 0.001) return diff;
      return String(b.addedAt || '').localeCompare(String(a.addedAt || ''));
    });
    return ranked.slice(0, homeSectionCaps.added);
  }, [dedupedBaseItems, homeSectionCaps.added, scoreHomeRelevance]);

  const renderHomeSection = (title: string, items: MediaItem[], subtitle?: string) => {
    const gridItems = dedupeSeriesForGrid(items || []);
    if (!gridItems || gridItems.length === 0) return null;
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-xl font-semibold text-white">{title}</h3>
            {subtitle ? <div className="text-xs text-slate-400 mt-1">{subtitle}</div> : null}
          </div>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-2 sm:gap-4 lg:gap-6">
          {gridItems.map((item) => (
            <MediaCard key={item.id} item={item} onClick={onSelectMedia} />
          ))}
        </div>
      </div>
    );
  };

  if (currentView === 'settings') {
    if (!currentUser?.isManager) {
      return (
        <div className="flex flex-col items-center justify-center py-20 text-slate-400">
          <div className="w-16 h-16 bg-slate-800 rounded-full flex items-center justify-center mb-4">
            <ServerCrash size={32} />
          </div>
          <h2 className="text-xl font-semibold text-white mb-2">{t('access_restricted_title')}</h2>
          <p className="mb-6 text-center max-w-md">{t('access_settings_denied')}</p>
          <Button onClick={() => onChangeView('home')}>{t('back')}</Button>
        </div>
      );
    }
    return (
      <Suspense fallback={<InlineLoader label={t('loading')} />}>
        <SettingsView
          drives={drives}
          libraryPaths={libraryPaths}
          looseMovieRoots={looseMovieRoots}
          rootTypes={rootTypes}
          scanStatus={scanStatus}
          enrichment={enrichment}
          migrationRunning={migrationRunning}
          migrationError={migrationError}
          onStartEnrich={onStartEnrich}
          metadataConfig={metadataConfig}
          profiles={profiles}
          onAddPath={onAddPath}
          onRemovePath={onRemovePath}
          onUpdateConfig={onUpdateConfig}
          onToggleLooseMovieRoot={onToggleLooseMovieRoot}
          onSetRootType={onSetRootType}
          onStartScan={onStartScan}
          onRefresh={onRefreshSettings}
          onAddProfile={onAddProfile}
          onUpdateProfile={onUpdateProfile}
          onDeleteProfile={onDeleteProfile}
        />
      </Suspense>
    );
  }

  if (currentView === 'library') {
    return <LibraryOverview onSelectMedia={onSelectMedia} />;
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-slate-400">
        <ServerCrash size={64} className="mb-4 text-red-500 opacity-50" />
        <h2 className="text-xl font-bold text-white mb-2">{t('connection_problem')}</h2>
        <p className="mb-6">{error}</p>
        <Button onClick={onRefreshMedia}>{t('retry')}</Button>
      </div>
    );
  }

  if (!loading && baseItems.length === 0 && searchQuery) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-slate-400">
        <div className="w-16 h-16 bg-slate-800 rounded-full flex items-center justify-center mb-4">
          <Search size={32} />
        </div>
        <h2 className="text-xl font-semibold text-white mb-2">{t('no_results_title')}</h2>
        <p>{t('no_results_for')} "{searchQuery}"</p>
        {suggestedItems.length > 0 ? (
          <div className="w-full mt-6">
            <div className="text-sm text-slate-300 mb-3">{t('did_you_mean')}</div>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-2 sm:gap-4 lg:gap-6">
              {suggestedItems.map((it) => (
                <MediaCard key={`suggest-${it.id}`} item={it} onClick={onSelectMedia} />
              ))}
            </div>
          </div>
        ) : null}
      </div>
    );
  }

  if (!loading && currentView === 'list' && baseItems.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-slate-400">
        <div className="w-16 h-16 bg-slate-800 rounded-full flex items-center justify-center mb-4">
          <Search size={32} />
        </div>
        <h2 className="text-xl font-semibold text-white mb-2">{t('list_empty_title')}</h2>
        <p>{t('list_empty_desc')}</p>
      </div>
    );
  }

  if ((profileLoadingHint || loading) && currentView === 'home' && !searchQuery) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-slate-400">
        <Loader2 size={32} className="mb-4 text-indigo-400 animate-spin" />
        <h2 className="text-xl font-semibold text-white mb-2">
          {t('loading_recommendations_title')}
        </h2>
        <p>{t('loading_recommendations_desc')}</p>
      </div>
    );
  }

  if (!loading && currentView === 'home' && !searchQuery && baseItems.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-slate-400">
        <div className="w-16 h-16 bg-slate-800 rounded-full flex items-center justify-center mb-4">
          <Search size={32} />
        </div>
        <h2 className="text-xl font-semibold text-white mb-2">{t('no_content_title')}</h2>
        <p>{t('no_content_desc')}</p>
      </div>
    );
  }

  const isHomeView = currentView === 'home' && !searchQuery;
  const gridSourceItems = isHomeView ? homeAddedItems : sortedMediaItems;

  return (
    <div className="space-y-6 sm:space-y-8 animate-fade-in relative max-w-[2000px] mx-auto px-3 sm:px-4 md:px-6 xl:px-8 pb-20 md:pb-0">
      {loading && baseItems.length > 0 && (
        <div className="absolute inset-0 z-10 bg-[#0b1220]/50 backdrop-blur-sm flex items-start justify-center pt-20">
          <div className="flex items-center space-x-2 bg-slate-800 px-4 py-2 rounded-full shadow-lg border border-white/10">
            <Loader2 className="animate-spin text-indigo-400" size={16} />
            <span className="text-sm font-medium text-white">{t('updating')}</span>
          </div>
        </div>
      )}

      {currentView === 'home' && !searchQuery && featuredItem && (
        <FeaturedHero
          item={featuredItem}
          onPlay={onPlay}
          onInfo={onSelectMedia}
          eyebrow={featuredHeroMeta.eyebrow}
          insight={featuredHeroMeta.insight}
          totalVariants={featuredHeroPool.length}
          activeVariant={featuredHeroIndex}
        />
      )}

      {isHomeView ? (
        <div className="space-y-7 sm:space-y-10">
          {renderHomeSection(
            t('home_recommended_title'),
            homeRecommendedItems,
            t('home_recommended_subtitle'),
          )}
          {renderHomeSection(
            t('home_airing_anime_title'),
            homeAiringAnimeItems,
            t('home_airing_anime_subtitle'),
          )}
          {renderHomeSection(
            t('home_airing_series_title'),
            homeAiringSeriesItems,
            t('home_airing_series_subtitle'),
          )}
          {renderHomeSection(t('home_in_progress_title'), homeInProgressItems, t('home_in_progress_subtitle'))}
          {renderHomeSection(t('home_trending_title'), homeTrendingItems, t('home_trending_subtitle'))}
        </div>
      ) : null}

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h2 className="text-xl sm:text-2xl font-bold text-white">
          {searchQuery
            ? `${t('results_for')} "${searchQuery}"`
            : currentView === 'home'
              ? t('recently_added')
              : currentView === 'movies'
                ? t('all_movies')
                : currentView === 'list'
                  ? t('my_list')
                  : t('series_and_tv')}
        </h2>

        <div className="flex items-center gap-2 sm:gap-3">
          {currentUser?.isKid && (
            <div className="hidden sm:flex items-center px-3 py-1.5 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-emerald-300 text-xs font-semibold">
              {t('kid_mode_active')}
            </div>
          )}
          {!isHomeView ? (
            <div className="flex items-center space-x-2 bg-slate-800/50 rounded-lg px-3 py-1.5 border border-white/5 hover:bg-slate-800 transition-colors">
              <ArrowUpDown size={14} className="text-slate-400" />
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as SortOption)}
                className="bg-transparent text-sm text-slate-200 focus:outline-none cursor-pointer [&>option]:bg-[#1e293b]"
              >
                <option value="date">{t('sort_added')}</option>
                <option value="title">{t('sort_title')}</option>
                <option value="rating">{t('sort_rating')}</option>
              </select>
            </div>
          ) : null}
          <Button variant="ghost" size="sm" icon={<RefreshCw size={14} />} onClick={onRefreshMedia}>
            <span className="hidden sm:inline">{t('refresh')}</span>
          </Button>
        </div>
      </div>

      {loading && baseItems.length === 0 && currentView !== 'list' ? (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-2 sm:gap-4 lg:gap-6 pb-16 sm:pb-20">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="flex flex-col space-y-3 animate-pulse">
              <div className="w-full aspect-[2/3] bg-slate-800/50 rounded-xl border border-white/5" />
              <div className="px-1 space-y-2">
                <div className="h-4 bg-slate-800/50 rounded w-3/4" />
              </div>
            </div>
          ))}
        </div>
      ) : (
        <>
          <div className="pb-20 md:pb-0">
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-2 sm:gap-4 lg:gap-6">
              {gridSourceItems.filter(Boolean).map((item) => (
                <MediaCard key={item.id} item={item} onClick={onSelectMedia} />
              ))}
            </div>
          </div>
          {currentView === 'list' && userListLoading ? <InlineLoader label={t('loading_list')} /> : null}
          {!isHomeView ? (
            <div className="flex items-center justify-center py-4 min-h-[52px]">
              {loadingMoreMedia ? (
                <InlineLoader label={t('loading_more')} />
              ) : hasMoreMedia ? (
                <Button
                  size="sm"
                  onClick={(e) => {
                    try { (e.currentTarget as HTMLButtonElement).blur(); } catch {}
                    onLoadMoreMedia();
                  }}
                >
                  {t('load_more')}
                </Button>
              ) : null}
            </div>
          ) : null}
          {!isHomeView ? <div ref={sentinelRef} className="h-2" /> : null}
          {!isHomeView && !hasMoreMedia && gridSourceItems.length > 0 ? (
            <div className="flex items-center justify-center py-6 text-xs text-slate-500">
              {t('showing')} {gridSourceItems.length}
              {currentView === 'list' ? '' : mediaTotal ? ` ${t('of')} ${mediaTotal}` : ''}.
            </div>
          ) : null}
        </>
      )}
    </div>
  );
};
