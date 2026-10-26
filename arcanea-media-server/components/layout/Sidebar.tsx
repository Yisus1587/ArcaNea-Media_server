import React, { useState, useRef, useEffect } from 'react';
import { Home, Film, Tv, Settings, Search, LogOut, X, ChevronUp, Check, User, Pin, PinOff, Bookmark, ChevronLeft } from 'lucide-react';
import { ViewState, UserProfile } from '../../types';
import { useI18n } from '../../i18n/i18n';

interface SidebarProps {
  currentView: ViewState;
  onChangeView: (view: ViewState) => void;
  isOpen: boolean;
  onRequestOpen?: () => void;
  onRequestClose?: () => void;
  pinned: boolean;
  onTogglePinned: () => void;
  currentUser: UserProfile | null;
  onLogout: () => void;
  searchQuery: string;
  onSearch: (query: string) => void;
  profiles: UserProfile[];
  onSwitchProfile: (profile: UserProfile) => void;
  scanRunning?: boolean;
  scanProgress?: number | null;
}

export const Sidebar: React.FC<SidebarProps> = ({ 
  currentView, 
  onChangeView, 
  isOpen,
  onRequestOpen,
  onRequestClose,
  pinned,
  onTogglePinned,
  currentUser, 
  onLogout,
  searchQuery,
  onSearch,
  profiles,
  onSwitchProfile,
  scanRunning,
  scanProgress,
}) => {
  const { t } = useI18n();
  const [isProfileMenuOpen, setIsProfileMenuOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const profileMenuRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const requestFocusSearchRef = useRef(false);

  useEffect(() => {
    const mq = window.matchMedia('(max-width: 639px)');
    const apply = () => setIsMobile(!!mq.matches);
    apply();
    try {
      mq.addEventListener('change', apply);
      return () => mq.removeEventListener('change', apply);
    } catch (e) {
      // Safari/old fallback
      // eslint-disable-next-line deprecation/deprecation
      mq.addListener(apply);
      // eslint-disable-next-line deprecation/deprecation
      return () => mq.removeListener(apply);
    }
  }, []);

  // Close menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (profileMenuRef.current && !profileMenuRef.current.contains(event.target as Node)) {
        setIsProfileMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // If we programmatically requested opening the sidebar for search, focus the input after it renders.
  useEffect(() => {
    if (!isOpen) return;
    if (!requestFocusSearchRef.current) return;
    requestFocusSearchRef.current = false;
    window.setTimeout(() => {
      searchInputRef.current?.focus();
    }, 60);
  }, [isOpen]);

  const navItems = [
    { id: 'home', label: t('nav_home'), icon: Home },
    { id: 'list', label: t('nav_list'), icon: Bookmark },
    { id: 'movies', label: t('nav_movies'), icon: Film },
    { id: 'tv', label: t('nav_series'), icon: Tv },
    ...(currentUser?.isManager ? [{ id: 'settings', label: t('nav_settings'), icon: Settings }] : []),
  ];

  return (
    <>
    <aside 
      data-arcanea-sidebar="1"
      className={`hidden sm:flex fixed top-0 left-0 h-full z-40 bg-[#0b1220]/95 backdrop-blur-xl border-r border-white/5 transition-all duration-300 flex-col pt-[env(safe-area-inset-top)] pl-[env(safe-area-inset-left)] ${
        isOpen ? 'w-56 sm:w-64' : 'w-16 sm:w-20'
      }`}
    >
      {/* Logo Area */}
      <div className="h-16 sm:h-20 flex items-center px-4 sm:px-6 border-b border-white/5">
        <button
          type="button"
          onClick={() => {
            if (!isMobile) return;
            if (isOpen) onRequestClose?.();
            else onRequestOpen?.();
          }}
          className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-indigo-500/20 shrink-0"
          aria-label={t('sidebar_open')}
        >
          <img src="/icons/arcanea-icon.svg" alt="ArcaNea" className="w-5 h-5" />
        </button>
        <span className={`ml-3 font-bold text-xl tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-white to-slate-400 transition-all duration-300 ${isOpen ? 'opacity-100 w-auto' : 'opacity-0 w-0 overflow-hidden'}`}>
          ArcaNea
        </span>
        {isOpen && (
          <div className="ml-auto flex items-center">
            {scanRunning ? (
              <div className="hidden sm:flex items-center gap-2 text-[11px] text-slate-300 bg-white/5 border border-white/10 rounded-full px-2 py-1 mr-2">
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-indigo-500 animate-pulse" />
                {t('scanning')}
              </div>
            ) : null}
            {isOpen && (
              <button
                type="button"
                onClick={() => onRequestClose?.()}
                className="p-2 rounded-lg transition-colors border bg-white/0 border-white/5 text-slate-400 hover:text-white hover:bg-white/5 mr-2"
                title={t('sidebar_close')}
              >
                <ChevronLeft size={16} />
              </button>
            )}
            {isMobile && !pinned ? (
              <button
                type="button"
                onClick={() => onRequestClose?.()}
                className="p-2 rounded-lg transition-colors border bg-white/0 border-white/5 text-slate-400 hover:text-white hover:bg-white/5 mr-2"
                title={t('sidebar_close')}
              >
                <X size={16} />
              </button>
            ) : null}
            <button
              type="button"
              onClick={onTogglePinned}
              className={`p-2 rounded-lg transition-colors border ${
                pinned
                  ? 'bg-indigo-600/10 border-indigo-500/20 text-indigo-300 hover:bg-indigo-600/20'
                  : 'bg-white/0 border-white/5 text-slate-400 hover:text-white hover:bg-white/5'
              }`}
              title={pinned ? t('sidebar_unpin') : t('sidebar_pin')}
            >
              {pinned ? <PinOff size={16} /> : <Pin size={16} />}
            </button>
          </div>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-6 space-y-2 px-3 overflow-hidden">
        {/* Persistent Search Bar */}
        <div className="px-3 mb-6 min-h-[40px]">
          {isOpen ? (
            <div className="relative group animate-fade-in">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 group-focus-within:text-indigo-400 pointer-events-none" size={16} />
              <input 
                ref={searchInputRef}
                type="text" 
                placeholder={t('search_placeholder')} 
                value={searchQuery}
                onChange={(e) => onSearch(e.target.value)}
                onBlur={() => {
                  if (!pinned && onRequestClose) {
                    // In mobile, collapse after leaving search to recover space.
                    if (isMobile) onRequestClose();
                    // On desktop, collapse only if query is cleared.
                    else if (!searchQuery) onRequestClose();
                  }
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    e.preventDefault();
                    if (!pinned) onRequestClose?.();
                  }
                }}
                className="w-full bg-slate-800/50 border border-white/5 rounded-lg py-2 pl-10 pr-8 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-indigo-500/50 transition-all"
              />
              {searchQuery && (
                <button 
                  onClick={() => {
                    onSearch('');
                    searchInputRef.current?.focus();
                  }}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-white p-1 rounded-full hover:bg-white/10 transition-colors"
                >
                  <X size={14} />
                </button>
              )}
            </div>
          ) : (
               <div className="flex justify-center group relative">
                  <div 
                    className="p-2.5 rounded-xl text-slate-400 hover:text-white hover:bg-white/5 transition-colors cursor-pointer"
                   onClick={() => {
                     requestFocusSearchRef.current = true;
                     onRequestOpen?.();
                   }}
                  >
                     <Search size={20} />
                  </div>
                {/* Tooltip for collapsed search */}
                <div className="absolute left-full top-1/2 -translate-y-1/2 ml-4 px-2 py-1 bg-slate-800 text-xs text-white rounded opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity whitespace-nowrap z-50 border border-white/10">
                  {t('search')}
                </div>
             </div>
          )}
        </div>

        {navItems.map((item) => {
          const isActive = currentView === item.id;
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              onClick={() => {
                onChangeView(item.id as ViewState);
                if (isMobile && !pinned) onRequestClose?.();
              }}
              className={`w-full flex items-center px-3 py-2 sm:py-3 rounded-xl transition-all duration-200 group relative overflow-hidden ${
                isActive 
                  ? 'bg-indigo-600/10 text-indigo-400' 
                  : 'text-slate-400 hover:text-white hover:bg-white/5'
              }`}
            >
              {isActive && (
                <div className="absolute left-0 top-0 bottom-0 w-1 bg-indigo-500 rounded-r-full" />
              )}
              <Icon size={18} className={`${isActive ? 'text-indigo-400' : 'text-slate-400 group-hover:text-white'} transition-colors shrink-0`} />
              <span className={`ml-3 font-medium transition-all duration-300 whitespace-nowrap ${isOpen ? 'opacity-100 translate-x-0' : 'opacity-0 -translate-x-4 absolute'}`}>
                {item.label}
              </span>
              
              {/* Tooltip for collapsed state */}
              {!isOpen && (
                <div className="absolute left-full top-1/2 -translate-y-1/2 ml-4 px-2 py-1 bg-slate-800 text-xs text-white rounded opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity whitespace-nowrap z-50 border border-white/10">
                  {item.label}
                </div>
              )}
            </button>
          );
        })}
      </nav>

      {/* User & Logout - Quick Switcher */}
      <div className="p-4 border-t border-white/5 relative" ref={profileMenuRef}>
        
        {/* Profile Menu Dropdown */}
        {isProfileMenuOpen && (
          <div className={`absolute bottom-full left-4 right-4 mb-2 bg-[#1e293b] border border-white/10 rounded-xl shadow-2xl overflow-hidden animate-slide-up z-50 ${!isOpen ? 'left-16 w-56 bottom-0' : ''}`}>
             <div className="p-2 space-y-1">
                <div className="px-2 py-1.5 text-xs font-semibold text-slate-500 uppercase tracking-wider">{t('switch_profile')}</div>
                <div className="h-px bg-white/5 my-1" />
                {profiles.map(profile => (
                  <button
                    key={profile.id}
                    onClick={() => {
                      if (profile.id !== currentUser?.id) {
                         onSwitchProfile(profile);
                      }
                      setIsProfileMenuOpen(false);
                    }}
                    className={`w-full flex items-center px-2 py-2 rounded-lg transition-colors ${profile.id === currentUser?.id ? 'bg-indigo-600/20 text-indigo-300' : 'hover:bg-white/5 text-slate-300 hover:text-white'}`}
                  >
                    <div className={`w-6 h-6 rounded-md flex items-center justify-center mr-3 overflow-hidden ${profile.avatarColor}`}>
                      {profile.avatarImage ? (
                        <img src={profile.avatarImage} alt={profile.name} className="w-full h-full object-cover" loading="lazy" />
                      ) : (
                        <span className="text-xs font-bold text-white">{profile.name[0]}</span>
                      )}
                    </div>
                    <span className="text-sm font-medium flex-1 text-left">{profile.name}</span>
                    {profile.id === currentUser?.id && <Check size={14} />}
                  </button>
                ))}
                <div className="h-px bg-white/5 my-1" />
                <button 
                  onClick={() => {
                    onLogout();
                    setIsProfileMenuOpen(false);
                  }}
                  className="w-full flex items-center px-2 py-2 rounded-lg text-red-400 hover:bg-red-500/10 transition-colors text-sm"
                >
                   <LogOut size={16} className="mr-3" />
                   {t('logout')}
                </button>
             </div>
          </div>
        )}

        {/* User Button */}
        <button 
          onClick={() => setIsProfileMenuOpen(!isProfileMenuOpen)}
          className={`w-full flex items-center p-2 rounded-xl border border-transparent hover:bg-slate-800/50 hover:border-white/5 transition-all duration-300 ${!isOpen ? 'justify-center' : ''} ${isProfileMenuOpen ? 'bg-slate-800 border-white/5' : ''}`}
        >
          <div className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 shadow-lg overflow-hidden ${currentUser?.avatarColor || 'bg-slate-700'}`}>
            {currentUser?.avatarImage ? (
              <img src={currentUser.avatarImage} alt={currentUser.name} className="w-full h-full object-cover" loading="lazy" />
            ) : (
              <span className="font-bold text-white text-xs">{currentUser?.name?.[0] || 'U'}</span>
            )}
          </div>
          
          <div className={`ml-3 overflow-hidden transition-all duration-300 text-left flex-1 ${isOpen ? 'w-auto opacity-100' : 'w-0 opacity-0 hidden'}`}>
            <p className="text-sm font-bold text-white truncate">{currentUser?.name || t('user')}</p>
            <p className="text-[10px] text-slate-400 truncate">{currentUser?.isKid ? t('kid_profile') : t('standard_profile')}</p>
          </div>
          
          {isOpen && (
             <ChevronUp size={16} className={`text-slate-500 transition-transform duration-300 ${isProfileMenuOpen ? 'rotate-180' : ''}`} />
          )}
        </button>
      </div>
    </aside>

    {/* Mobile header + bottom navigation */}
    {isMobile ? (
      <>
        <div className="fixed top-0 left-0 right-0 z-50 bg-[#0b1220]/80 backdrop-blur-xl border-b border-white/5 pt-[env(safe-area-inset-top)] pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)]">
          <div className="h-14 flex items-center justify-between px-4">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-indigo-500/20">
                <img src="/icons/arcanea-icon.svg" alt="ArcaNea" className="w-5 h-5" />
              </div>
              <div className="text-xl font-bold tracking-tight text-white">
                ArcaNea
              </div>
              {scanRunning ? (
                <div className="flex items-center gap-1.5 text-[10px] font-bold text-indigo-400 bg-indigo-500/10 border border-indigo-500/20 rounded-full px-2 py-0.5 animate-pulse">
                  <span className="w-1 h-1 rounded-full bg-indigo-400" />
                  {t('scanning')}
                </div>
              ) : null}
            </div>
            <button
              type="button"
              onClick={() => {
                requestFocusSearchRef.current = true;
                onRequestOpen?.();
              }}
              className={`p-2.5 rounded-xl transition-all ${isOpen ? 'bg-indigo-500 text-white' : 'text-slate-300 bg-white/5 border border-white/5'}`}
              aria-label={t('search')}
            >
              <Search size={20} />
            </button>
          </div>
        </div>

        {isOpen ? (
          <>
            <div className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm" onClick={() => onRequestClose?.()} aria-hidden="true" />
            <div className="fixed left-0 right-0 z-50 p-4 bg-[#0b1220]/95 backdrop-blur-2xl border-b border-white/5 animate-in slide-in-from-top duration-300 top-[calc(3.5rem+env(safe-area-inset-top,0px))]">
              <div className="relative group">
                <Search className="absolute left-4 top-1/2 -translate-y-1/2 text-slate-400 group-focus-within:text-indigo-400 transition-colors" size={18} />
                <input 
                  ref={searchInputRef}
                  type="text" 
                  placeholder={t('search_placeholder')} 
                  value={searchQuery}
                  onChange={(e) => onSearch(e.target.value)}
                  className="w-full bg-white/5 border border-white/10 rounded-2xl py-3.5 pl-12 pr-12 text-base text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-all shadow-inner"
                />
                {searchQuery && (
                  <button 
                    onClick={() => {
                      onSearch('');
                      searchInputRef.current?.focus();
                    }}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-white p-1.5 rounded-full bg-white/5 transition-colors"
                  >
                    <X size={16} />
                  </button>
                )}
              </div>
            </div>
          </>
        ) : null}

        {isProfileMenuOpen && (
          <div className="fixed inset-0 z-40" onClick={() => setIsProfileMenuOpen(false)} aria-hidden="true" />
        )}
        {isProfileMenuOpen && (
          <div ref={profileMenuRef} className="fixed bottom-20 left-4 right-4 z-50 bg-slate-900/95 backdrop-blur-2xl border border-white/10 rounded-3xl shadow-[0_20px_50px_rgba(0,0,0,0.5)] overflow-hidden animate-slide-up">
            <div className="p-4 space-y-4">
              <div className="flex items-center gap-4 px-2 pb-2 border-b border-white/5">
                <div className={`w-12 h-12 rounded-2xl flex items-center justify-center shrink-0 shadow-lg overflow-hidden ${currentUser?.avatarColor || 'bg-slate-700'}`}>
                  {currentUser?.avatarImage ? (
                    <img src={currentUser.avatarImage} alt={currentUser.name} className="w-full h-full object-cover" />
                  ) : (
                    <span className="font-bold text-white text-lg">{currentUser?.name?.[0] || 'U'}</span>
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-base font-bold text-white truncate">{currentUser?.name || t('user')}</p>
                  <p className="text-xs text-slate-400 truncate">{currentUser?.isKid ? t('kid_profile') : t('standard_profile')}</p>
                </div>
              </div>

              <div className="space-y-1">
                <div className="px-2 py-1 text-[10px] font-bold text-slate-500 uppercase tracking-widest">{t('switch_profile')}</div>
                <div className="grid grid-cols-2 gap-2">
                  {profiles.map(profile => (
                    <button
                      key={profile.id}
                      onClick={() => {
                        if (profile.id !== currentUser?.id) {
                          onSwitchProfile(profile);
                        }
                        setIsProfileMenuOpen(false);
                      }}
                      className={`flex flex-col items-center gap-2 p-3 rounded-2xl transition-all ${profile.id === currentUser?.id ? 'bg-indigo-600/20 ring-1 ring-indigo-500/30' : 'bg-white/5 hover:bg-white/10'}`}
                    >
                      <div className={`w-10 h-10 rounded-xl flex items-center justify-center overflow-hidden ${profile.avatarColor}`}>
                        {profile.avatarImage ? (
                          <img src={profile.avatarImage} alt={profile.name} className="w-full h-full object-cover" />
                        ) : (
                          <span className="text-sm font-bold text-white">{profile.name[0]}</span>
                        )}
                      </div>
                      <span className={`text-xs font-semibold truncate w-full text-center ${profile.id === currentUser?.id ? 'text-indigo-300' : 'text-slate-300'}`}>{profile.name}</span>
                    </button>
                  ))}
                </div>
              </div>

              <div className="pt-2 border-t border-white/5">
                <button 
                  onClick={() => {
                    onLogout();
                    setIsProfileMenuOpen(false);
                  }}
                  className="w-full flex items-center justify-center gap-3 py-3 rounded-2xl bg-red-500/10 text-red-400 active:scale-95 transition-all font-bold text-sm"
                >
                   <LogOut size={18} />
                   {t('logout')}
                </button>
              </div>
            </div>
          </div>
        )}
        <nav className="fixed bottom-0 left-0 right-0 z-50 bg-[#0b1220]/95 backdrop-blur-xl border-t border-white/5 pb-[env(safe-area-inset-bottom)] pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)]">
          <div className="h-16 flex items-center justify-around px-2">
            {navItems.map((item) => {
              const Icon = item.icon;
              const active = currentView === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => {
                    onChangeView(item.id as ViewState);
                    setIsProfileMenuOpen(false);
                  }}
                  className={`flex flex-col items-center justify-center min-w-[60px] h-full transition-all active:scale-90 ${active ? 'text-indigo-400' : 'text-slate-400'}`}
                >
                  <div className={`p-1.5 rounded-xl transition-colors ${active ? 'bg-indigo-500/10' : ''}`}>
                    <Icon size={22} className={active ? 'text-indigo-400' : 'text-slate-400'} />
                  </div>
                  <span className={`text-[10px] mt-0.5 font-medium transition-colors ${active ? 'text-indigo-400' : 'text-slate-500'}`}>
                    {item.label}
                  </span>
                </button>
              );
            })}
            <button
              onClick={() => setIsProfileMenuOpen(!isProfileMenuOpen)}
              className={`flex flex-col items-center justify-center min-w-[60px] h-full transition-all active:scale-90 ${isProfileMenuOpen ? 'text-indigo-400' : 'text-slate-400'}`}
            >
              <div className={`p-1.5 rounded-xl transition-colors ${isProfileMenuOpen ? 'bg-indigo-500/10' : ''}`}>
                <User size={22} />
              </div>
              <span className={`text-[10px] mt-0.5 font-medium ${isProfileMenuOpen ? 'text-indigo-400' : 'text-slate-500'}`}>
                {t('profile')}
              </span>
            </button>
          </div>
        </nav>
      </>
    ) : null}
    </>
  );
};
