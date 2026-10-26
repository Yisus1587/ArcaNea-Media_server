import { Suspense, lazy, useState, useEffect, useMemo, useCallback } from 'react';
import { Sidebar } from './components/layout/Sidebar';
import { ErrorBoundary } from './components/system/ErrorBoundary';
import { SplashScreen } from './components/ui/SplashScreen';
import { ProfileSelector } from './components/auth/ProfileSelector';
import { SetupWizard } from './components/setup/SetupWizard';
import { NotificationHost } from './components/ui/NotificationHost';
import { AppContent } from './components/app/AppContent';
import { MediaService, normalizeRoots, setAdminMode } from './services/api';
import { MediaItem, ViewState, MetadataConfig, AppStage, UserProfile } from './types';
import { Button } from './components/ui/Button';
import { defaultMetadataLanguageForUiLang, detectSystemLocale, detectSystemUiLang, getStoredUiLang, getStoredUiLangSource, normalizeMetadataLanguageFromLocale, useI18n } from './i18n/i18n';
import { useNotifications } from './hooks/useNotifications';
import { useRecommendations } from './hooks/useRecommendations';
import { usePlayerState } from './hooks/usePlayerState';
import { useConfigState } from './hooks/useConfigState';
import { useProfilesState } from './hooks/useProfilesState';
import { useSettingsState } from './hooks/useSettingsState';
import { useMediaLibrary } from './hooks/useMediaLibrary';

const MediaDetailModal = lazy(() =>
  import('./components/media/MediaDetailModal').then((module) => ({ default: module.MediaDetailModal })),
);
const VideoPlayer = lazy(() =>
  import('./components/player/VideoPlayer').then((module) => ({ default: module.VideoPlayer })),
);

function App() {
  const { lang, setLang, t } = useI18n();
  // UI locale is separate from provider metadata locale: TMDB doesn't reliably accept `es-419`.
  const defaultUiLocale = (detectSystemUiLang() === 'es') ? 'es-419' : 'en-US';
  const defaultProviderLanguage = defaultMetadataLanguageForUiLang(lang);
  const { notification, showNotification, clearNotification } = useNotifications();
  const {
    metadataConfig,
    setMetadataConfig,
    libraryPaths,
    setLibraryPaths,
    looseMovieRoots,
    setLooseMovieRoots,
  } = useConfigState(defaultProviderLanguage);
  // System State
  const [appStage, setAppStage] = useState<AppStage>('booting');
  const [currentUser, setCurrentUser] = useState<UserProfile | null>(null);

  // App State
  const [currentView, setCurrentView] = useState<ViewState>('home');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarPinned, setSidebarPinned] = useState(false);
  const [sidebarCrashed, setSidebarCrashed] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [pinPromptProfile, setPinPromptProfile] = useState<UserProfile | null>(null);
  const [pinPromptMode, setPinPromptMode] = useState<'select' | 'switch' | null>(null);
  const [pinValue, setPinValue] = useState('');
  const [pinError, setPinError] = useState<string | null>(null);
  
  // Status States (handled by media hook)
  
  // UI States
  const [selectedMedia, setSelectedMedia] = useState<MediaItem | null>(null);

  const {
    scrollContainerRef,
    sentinelRef,
    mediaItems,
    userListItems,
    userListLoading,
    mediaTotal,
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
  } = useMediaLibrary({
    appStage,
    currentUser,
    currentView,
    searchQuery,
    lang,
    setCurrentView,
    selectedMediaId: selectedMedia?.id,
    onSelectedMediaUpdate: (item) => setSelectedMedia(item),
  });

  const {
    profiles,
    setProfiles,
    handleAddProfile,
    handleUpdateProfile,
    handleDeleteProfile,
  } = useProfilesState({
    appStage,
    metadataConfig,
    libraryPaths,
    looseMovieRoots,
    currentUser,
    setCurrentUser,
    onLogout: handleLogout,
    showNotification,
  });

  const {
    drives,
    scanStatus,
    enrichment,
    serverOffline,
    migrationRunning,
    migrationError,
    fetchSettingsData,
    handleStartScan,
    handleStartEnrich,
    handleAddPath,
    handleRemovePath,
    handleUpdateMetadataConfig,
    handleToggleLooseMovieRoot,
    rootTypes,
    handleSetRootType,
  } = useSettingsState({
    appStage,
    currentUser,
    currentView,
    metadataConfig,
    setMetadataConfig,
    libraryPaths,
    setLibraryPaths,
    looseMovieRoots,
    setLooseMovieRoots,
    profiles,
    showNotification,
    fetchMedia,
    invalidateMediaCache,
    setLoading,
  });

  const [dismissOffline, setDismissOffline] = useState(false);
  useEffect(() => {
    if (!serverOffline) setDismissOffline(false);
  }, [serverOffline]);

  // Settings States handled via hooks

  // Keep metadata localization language aligned with UI language (per user choice),
  // and persist UI language preference to backend config.
  useEffect(() => {
    if (appStage !== 'app') return;
    try {
      let desired = defaultMetadataLanguageForUiLang(lang);
      const nextMeta =
        metadataConfig.language === desired
          ? metadataConfig
          : { ...metadataConfig, language: desired };

      if (nextMeta !== metadataConfig) {
        setMetadataConfig(nextMeta);
      }

      const storedSource = getStoredUiLangSource();
      const storedLang = getStoredUiLang();
      const uiSource = storedSource ?? (storedLang ? 'manual' : 'system');
      if (uiSource === 'system') {
        desired = normalizeMetadataLanguageFromLocale(detectSystemLocale());
      }

      void MediaService.saveAppConfig({
        setupComplete: true,
        profiles,
        metadata: nextMeta,
        media_roots: libraryPaths,
        loose_movie_roots: looseMovieRoots,
        target_lang: lang,
        ui_lang_source: uiSource,
      });
    } catch {
      // ignore
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lang, appStage]);

  // Layout self-heal: if the app thinks the sidebar is open but CSS/JS got out of sync (e.g. stale assets),
  // the main content can shift and leave a dead left gap. Measure the sidebar and clamp state accordingly.
  useEffect(() => {
    let cancelled = false;
    if (appStage !== 'app') return;
    if (sidebarCrashed) return;
    if (!sidebarOpen) return;

    const t = window.setTimeout(() => {
      try {
        const el = document.querySelector('[data-arcanea-sidebar="1"]') as HTMLElement | null;
        if (!el) return;
        const w = el.offsetWidth || 0;
        // "open" should be ~256px on desktop. If it's still collapsed, close to prevent layout gap.
        if (w > 0 && w < 160) setSidebarOpen(false);
      } catch {
        // ignore
      }
    }, 350);

    return () => window.clearTimeout(t);
  }, [appStage, sidebarCrashed, sidebarOpen]);

  // --- Boot Sequence Handlers ---
  const handleBootComplete = () => {
    // Check backend for persisted app config; route accordingly
    (async () => {
      try {
        const cfg = await MediaService.getAppConfig();
        if (cfg && cfg.setupComplete) {
          try {
            const tl = String(cfg.target_lang || '').toLowerCase();
            const src = String((cfg as any).ui_lang_source || '').toLowerCase();
            if (src === 'system') {
              setLang(detectSystemUiLang(), 'system');
            } else if (tl === 'es' || tl === 'en') {
              setLang(tl as 'es' | 'en', 'manual');
            }
          } catch {
            // ignore
          }
          // restore profiles and settings
          const profsRaw = cfg.profiles || [];
          const profs = Array.isArray(profsRaw) ? profsRaw : [];
          const managers = profs.filter((p: any) => !!p && !!p.isManager) as UserProfile[];
          const nonManagers = profs.filter((p: any) => !p || !p.isManager) as UserProfile[];
          const primaryManager: UserProfile | null = managers.length ? managers[0] : null;
          const fixedProfiles: UserProfile[] = primaryManager
            ? [primaryManager, ...nonManagers, ...managers.slice(1)]
            : ([
                {
                  id: `manage-${Date.now()}`,
                  name: t('management_profile_name'),
                  avatarColor: 'bg-indigo-600',
                  isKid: false,
                  isManager: true,
                  language: (profs[0] as any)?.language || defaultUiLocale,
                } as UserProfile,
                ...(profs as UserProfile[]),
              ]);
          setProfiles(fixedProfiles);
          // Always start in profile selector (no auto-login)
          setCurrentUser(null);
          if (cfg.metadata) setMetadataConfig(cfg.metadata);
          if (cfg.media_roots) setLibraryPaths(normalizeRoots(cfg.media_roots));
          if ((cfg as any).loose_movie_roots) setLooseMovieRoots(normalizeRoots((cfg as any).loose_movie_roots));
          setCurrentView('home');
          setSearchQuery('');
          setAppStage('profile_select');
          // Do not auto-start a scan on every app load.
          // Users can trigger scans explicitly from Settings.
        } else {
          setAppStage('setup');
        }
      } catch (e) {
        setAppStage('setup');
      }
    })();
  };

  const handleSetupComplete = (managerProfile: UserProfile, paths: string[], metadata: MetadataConfig) => {
    // Save configuration
    setProfiles([managerProfile]);
    setLibraryPaths(paths);
    setMetadataConfig(metadata);
    // Always show profile selector after setup (no auto-login)
    setCurrentUser(null);
    setCurrentView('home');
    setSearchQuery('');
    setAppStage('profile_select');
    
    // Notify and Start Scan
    showNotification('success', t('setup_complete_notice'));
    handleStartScan(paths);
  };

  const beginPinPrompt = (profile: UserProfile, mode: 'select' | 'switch') => {
    setPinPromptProfile(profile);
    setPinPromptMode(mode);
    setPinValue('');
    setPinError(null);
  };

  const commitProfile = async (profile: UserProfile, mode: 'select' | 'switch') => {
    if (!profile?.isManager) {
      try {
        await MediaService.adminLogout();
      } catch {
        // ignore
      }
    }
    setCurrentUser(profile);
    setAdminMode(!!profile?.isManager);
    setAppStage('app');
    setCurrentView('home');
    setSearchQuery('');
    resetMediaState();
    setProfileLoadingHint(true);
    if (mode === 'switch') {
      showNotification('success', `${t('notif_profile_switched')} ${profile.name}`);
    } else {
      showNotification('success', `${t('notif_welcome_back')}, ${profile.name}`);
    }
    await fetchMedia({ reset: true, userProfile: profile });
  };

  const requestProfileAccess = (profile: UserProfile, mode: 'select' | 'switch') => {
    const pin = (profile.pin || '').trim();
    const mustPrompt = !!pin || !!profile.isManager;
    if (mustPrompt) {
      beginPinPrompt(profile, mode);
      return;
    }
    void commitProfile(profile, mode);
  };

  const handlePinCancel = () => {
    setPinPromptProfile(null);
    setPinPromptMode(null);
    setPinValue('');
    setPinError(null);
  };

  const handlePinSubmit = async () => {
    const prof = pinPromptProfile;
    const mode = pinPromptMode;
    if (!prof || !mode) return;
    const expected = String(prof.pin || '').trim();
    const entered = String(pinValue || '').trim();
    if (prof.isManager) {
      if (!entered) {
        setPinError(t('pin_required'));
        return;
      }
      const res = await MediaService.adminLogin(entered);
      if (!res?.ok) {
        setPinError(t('pin_incorrect'));
        return;
      }
      if (expected && entered !== expected) {
        try {
          await handleUpdateProfile({ ...prof, pin: entered });
        } catch {
          // ignore
        }
      }
      handlePinCancel();
      await commitProfile(prof, mode);
      return;
    }
    if (!expected) {
      handlePinCancel();
      await commitProfile(prof, mode);
      return;
    }
    if (entered !== expected) {
      setPinError(t('pin_incorrect'));
      return;
    }
    handlePinCancel();
    await commitProfile(prof, mode);
  };

  const handleProfileSelect = (profile: UserProfile) => {
    requestProfileAccess(profile, 'select');
  };

  async function handleLogout() {
    try {
      await MediaService.adminLogout();
    } catch {
      // ignore
    }
    setCurrentUser(null);
    setAdminMode(false);
    setAppStage('profile_select');
    setSearchQuery('');
    setSidebarPinned(false);
    handlePinCancel();
    resetMediaState();
  }

  const handleQuickSwitchProfile = (profile: UserProfile) => {
    requestProfileAccess(profile, 'switch');
  };
  // --- Data Fetching ---
  useEffect(() => {
    if (appStage === 'app' && currentView === 'settings') {
      fetchSettingsData();
    }
  }, [currentView, appStage]);

  const handleViewChange = (view: ViewState) => {
    if (view === 'settings' && !currentUser?.isManager) {
      showNotification('info', t('access_settings_denied'));
      return;
    }
    setCurrentView(view);
    setSearchQuery(''); // Clear search when navigating categories
  };

  useEffect(() => {
    if (currentView === 'list') {
      void refreshUserList(currentUser);
    }
  }, [currentView, currentUser?.id]);

  const baseItems = useMemo(() => {
    if (currentView === 'list') return userListItems;
    if (searchQuery && localSearchActive && localSearchResults.length) return localSearchResults;
    return mediaItems;
  }, [currentView, userListItems, mediaItems, searchQuery, localSearchActive, localSearchResults]);

  const displayItems = useMemo(() => {
    if (currentView === 'list') return baseItems;
    if (!baseItems.length) return baseItems;
    const groups = new Map<string, MediaItem[]>();
    const standalone: MediaItem[] = [];
    for (const it of baseItems) {
      if (!it) continue;
      const seriesId = (it.seriesId ?? it.tmdbId) ? String(it.seriesId ?? it.tmdbId) : '';
      if (seriesId) {
        const key = `series:${seriesId}`;
        const arr = groups.get(key);
        if (arr) arr.push(it);
        else groups.set(key, [it]);
      } else {
        standalone.push(it);
      }
    }
    const pickRepresentative = (items: MediaItem[]) => {
      if (items.length === 1) return items[0];
      const withSeason = items.filter((i) => typeof i.seasonNumber === 'number' && !Number.isNaN(i.seasonNumber));
      if (withSeason.length) {
        withSeason.sort((a, b) => (a.seasonNumber ?? 0) - (b.seasonNumber ?? 0));
        return withSeason[0];
      }
      const byDate = [...items].sort((a, b) => (a.addedAt || '').localeCompare(b.addedAt || ''));
      return byDate[0];
    };
    const out: MediaItem[] = [];
    for (const items of groups.values()) {
      const rep = pickRepresentative(items);
      if (rep) out.push(rep);
    }
    return [...out, ...standalone];
  }, [baseItems, currentView]);

  const {
    recordPlay,
    inProgressItems,
    trendingItems,
    recommendedItems,
    recommendedForYou,
  } = useRecommendations({
    appStage,
    currentUser,
    currentView,
    searchQuery,
    baseItems: displayItems,
  });

  const {
    playingMedia,
    playingSrc,
    playerSeasons,
    nowPlayingLabel,
    playingStartTime,
    handlePlay,
    handleNextPlayingItem,
    handlePrevPlayingItem,
    handleContinueNextSeries,
    handleSelectEpisode,
    handleProgressUpdate,
    closePlayer,
    hasPrevItem,
    hasNextItem,
  } = usePlayerState({
    currentUser,
    lang,
    recordPlay,
    onCloseDetail: () => setSelectedMedia(null),
  });

  const handleMediaClick = useCallback((item: MediaItem) => {
    setSelectedMedia(item);
  }, []);

  const renderContent = () => (
    <AppContent
      currentView={currentView}
      currentUser={currentUser}
      drives={drives}
      libraryPaths={libraryPaths}
      scanStatus={scanStatus}
      enrichment={enrichment}
      migrationRunning={migrationRunning}
      migrationError={migrationError}
      metadataConfig={metadataConfig}
      profiles={profiles}
      error={error}
      loading={loading}
      profileLoadingHint={profileLoadingHint}
      searchQuery={searchQuery}
      lang={lang}
      baseItems={displayItems}
      userListLoading={userListLoading}
      suggestedItems={suggestedItems}
      hasMoreMedia={hasMoreMedia}
      loadingMoreMedia={loadingMoreMedia}
      mediaTotal={mediaTotal}
      scrollContainerRef={scrollContainerRef}
      sentinelRef={sentinelRef}
      recommendedItems={recommendedItems}
      recommendedForYou={recommendedForYou}
      inProgressItems={inProgressItems}
      trendingItems={trendingItems}
      onChangeView={handleViewChange}
      onStartEnrich={handleStartEnrich}
      onStartScan={() => handleStartScan()}
      onAddPath={handleAddPath}
      onRemovePath={handleRemovePath}
      onUpdateConfig={handleUpdateMetadataConfig}
      looseMovieRoots={looseMovieRoots}
      onToggleLooseMovieRoot={handleToggleLooseMovieRoot}
      rootTypes={rootTypes}
      onSetRootType={handleSetRootType}
      onRefreshSettings={fetchSettingsData}
      onAddProfile={handleAddProfile}
      onUpdateProfile={handleUpdateProfile}
      onDeleteProfile={handleDeleteProfile}
      onSelectMedia={handleMediaClick}
      onPlay={handlePlay}
      onRefreshMedia={() => fetchMedia({ reset: true })}
      onLoadMoreMedia={() => fetchMedia({ reset: false })}
    />
  );

  const renderStageContent = () => {
    if (appStage === 'booting') return <SplashScreen onComplete={handleBootComplete} />;
    if (appStage === 'setup') return <SetupWizard onComplete={handleSetupComplete} />;
    if (appStage === 'profile_select') {
      return (
        <ProfileSelector 
          profiles={profiles} 
          onSelect={handleProfileSelect} 
          onAdd={handleAddProfile}
          onEdit={handleUpdateProfile}
          onDelete={handleDeleteProfile}
        />
      );
    }
    return renderContent();
  };

  return (
    <div className="app-shell min-h-dvh pt-[env(safe-area-inset-top)] pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)] text-slate-100 flex overflow-hidden font-sans selection:bg-indigo-500/30 relative">
      {/* Offline Overlay */}
      {serverOffline && !dismissOffline ? (
        <div className="fixed inset-0 z-[999] bg-black/75 backdrop-blur-sm flex items-center justify-center p-6">
          <div className="w-full max-w-md rounded-2xl border border-white/10 bg-slate-950/90 shadow-2xl p-6">
            <div className="text-lg font-bold text-white">{t('server_offline_title')}</div>
            <div className="mt-2 text-sm text-slate-300">
              {t('server_offline_desc')}
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setDismissOffline(true)}
                className="px-3 py-2 rounded-lg bg-white/10 hover:bg-white/20 text-white text-sm font-semibold transition"
              >
                {t('server_offline_hide')}
              </button>
              <button
                onClick={() => window.location.reload()}
                className="px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-semibold transition"
              >
                {t('retry')}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {/* Global Loading Bar */}
      {loading && (
        <div className="absolute top-0 left-0 right-0 h-1 bg-slate-800 z-[100]">
          <div className="h-full bg-indigo-500 animate-progress-indeterminate shadow-[0_0_10px_rgba(99,102,241,0.5)]"></div>
        </div>
      )}

      {/* PIN Prompt */}
      {pinPromptProfile && (
        <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
          <div className="w-full max-w-sm rounded-2xl bg-slate-900/90 border border-white/10 shadow-2xl p-6">
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-lg font-semibold text-white">{t('pin_prompt_title')}</h3>
                <p className="text-sm text-slate-400 mt-1">
                  {t('profile_label')}: <span className="text-slate-200">{pinPromptProfile.name}</span>
                </p>
              </div>
            </div>

            <div className="mt-5">
              <input
                autoFocus
                type="password"
                inputMode="numeric"
                placeholder={t('pin_placeholder')}
                value={pinValue}
                onChange={(e) => {
                  const v = String(e.target.value || '').replace(/\D/g, '').slice(0, 6);
                  setPinValue(v);
                  setPinError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void handlePinSubmit();
                  if (e.key === 'Escape') handlePinCancel();
                }}
                className="w-full bg-slate-800/50 border border-white/10 rounded-xl py-3 px-4 text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/40"
              />
              {pinError && <div className="text-xs text-red-300 mt-2">{pinError}</div>}
            </div>

            <div className="mt-6 flex items-center justify-end gap-3">
              <Button variant="ghost" size="sm" onClick={handlePinCancel}>{t('cancel')}</Button>
              <Button size="sm" onClick={() => void handlePinSubmit()}>{t('enter')}</Button>
            </div>
          </div>
        </div>
      )}

	      {/* Conditionally render sidebar only in app mode */}
	      {appStage === 'app' && (
	        <ErrorBoundary
	          resetKey={`${currentUser?.id ?? 'no-user'}:${currentView}`}
	          onError={(e) => {
	            console.error('[ui] Sidebar crashed', e);
	            setSidebarCrashed(true);
	          }}
	          onReset={() => setSidebarCrashed(false)}
	          fallback={null}
	        >
	          <Sidebar 
	            currentView={currentView} 
	            onChangeView={handleViewChange} 
	            isOpen={sidebarOpen}
	            onRequestOpen={() => { setSidebarOpen(true); }}
	            onRequestClose={() => { if (!sidebarPinned) setSidebarOpen(false); }}
	            pinned={sidebarPinned}
	            onTogglePinned={() => setSidebarPinned(p => !p)}
	            currentUser={currentUser}
	            onLogout={handleLogout}
	            searchQuery={searchQuery}
	            onSearch={setSearchQuery}
	            profiles={profiles}
	            onSwitchProfile={handleQuickSwitchProfile}
	            scanRunning={!!scanStatus.scanning}
	            scanProgress={typeof scanStatus.progress === 'number' ? scanStatus.progress : null}
	          />
	        </ErrorBoundary>
	      )}
      
	      {/* Main Content Area */}
	      <main 
	        className={`flex-1 transition-all duration-300 ml-0 ${
	          appStage === 'app' && !sidebarCrashed ? (sidebarOpen ? 'sm:ml-64' : 'sm:ml-20') : ''
	        }`}
	      >
	        {/* Toggle Trigger Area - Only active in app mode */}
	        {appStage === 'app' && !sidebarCrashed && (
	          <div 
	            className="fixed left-0 top-0 bottom-0 w-20 z-30 hidden sm:block"
	            onMouseEnter={() => setSidebarOpen(true)}
	            onMouseLeave={() => { if (!sidebarPinned) setSidebarOpen(false); }}
	          />
	        )}

	        <div ref={scrollContainerRef} className={`h-full overflow-y-auto scroll-smooth ${appStage === 'app' ? 'p-6 md:p-10 pt-16 sm:pt-10' : 'p-0'}`}>
	           <ErrorBoundary
	             resetKey={`${appStage}:${currentView}:${searchQuery}:${currentUser?.id ?? 'no-user'}`}
	             onError={(e) => console.error('[ui] Stage content crashed', e)}
	             fallback={({ reset, error }) => (
	               <div className="w-full h-full flex flex-col items-center justify-center py-20 text-slate-300">
	                 <div className="text-lg font-bold text-white">{t('render_error_title')}</div>
	                 <div className="mt-2 text-sm text-slate-400 text-center max-w-md">
	                   {t('render_error_desc')}
	                 </div>
	                 {error ? (
	                   <pre className="mt-4 max-w-3xl text-xs text-rose-200/90 bg-rose-950/30 border border-rose-900/40 rounded-xl p-3 whitespace-pre-wrap break-words">
	                     {typeof error === 'string' ? error : (error as any)?.message || String(error)}
	                   </pre>
	                 ) : null}
	                 <button
	                   type="button"
	                   onClick={reset}
	                   className="mt-6 px-4 py-2 rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-sm text-white transition-colors"
	                 >
	                   {t('retry')}
	                 </button>
	               </div>
	             )}
	           >
	             {renderStageContent()}
	           </ErrorBoundary>
	        </div>
	      </main>

      {/* Global Toast Notifications */}
      <NotificationHost notification={notification} onClose={clearNotification} />

      {/* Modals */}
      {selectedMedia && (
        <Suspense fallback={<div className="fixed inset-0 z-[190] bg-black/50 backdrop-blur-sm" />}>
          <MediaDetailModal 
            item={selectedMedia} 
            onClose={() => setSelectedMedia(null)} 
            onPlay={handlePlay}
            isAdmin={!!currentUser?.isManager}
            onManualMappingSaved={(id) => refreshMediaItem(id)}
            profileId={currentUser?.id}
            onListChanged={() => {
              if (currentView === 'list') void refreshUserList(currentUser);
            }}
          />
        </Suspense>
      )}

      {playingMedia && (
        <Suspense fallback={<div className="fixed inset-0 z-[210] bg-black" />}>
          <VideoPlayer 
            src={playingSrc || ''} 
            title={playingMedia.title}
            nowPlayingLabel={nowPlayingLabel || undefined}
            startTime={playingStartTime || 0}
            onProgressUpdate={handleProgressUpdate}
            onPrevItem={hasPrevItem ? handlePrevPlayingItem : undefined}
            onNextItem={hasNextItem ? handleNextPlayingItem : undefined}
            hasPrevItem={hasPrevItem}
            hasNextItem={hasNextItem}
            episodeSeasons={playerSeasons.map(s => ({ key: s.key, label: s.label, episodes: s.episodes }))}
            currentPath={playingMedia.path}
            onSelectEpisode={handleSelectEpisode}
            nextSeriesTitle={(playingMedia as any)?.nextSeries?.title || undefined}
            onContinueNextSeries={
              (playingMedia as any)?.nextSeries?.id
                ? () => handleContinueNextSeries(Number((playingMedia as any).nextSeries.id))
                : undefined
            }
            onClose={closePlayer}
          />
        </Suspense>
      )}
    </div>
  );
}

export default App;
