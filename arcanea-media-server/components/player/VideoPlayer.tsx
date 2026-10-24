import React, { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import {
  X,
  Play,
  Pause,
  Volume2,
  Volume1,
  VolumeX,
  Maximize,
  Minimize,
  SkipBack,
  SkipForward,
  Settings,
  Subtitles,
  Loader2,
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  FastForward,
  Rewind,
  PictureInPicture,
  Info,
  List,
  Lock,
  Unlock,
  Clock,
} from 'lucide-react';
import { useI18n } from '../../i18n/i18n';

interface VideoPlayerProps {
  src: string;
  title: string;
  onClose: () => void;
  onPrevItem?: () => void;
  onNextItem?: () => void;
  hasPrevItem?: boolean;
  hasNextItem?: boolean;
  nowPlayingLabel?: string;
  nextSeriesTitle?: string;
  onContinueNextSeries?: () => void;
  episodeSeasons?: Array<{
    key: string;
    label: string;
    episodes: any[];
  }>;
  currentPath?: string;
  onSelectEpisode?: (seasonKey: string, episode: any) => void;
  poster?: string;
  subtitlesUrl?: string;
  autoPlay?: boolean;
  startTime?: number;
  onProgressUpdate?: (payload: { current: number; duration: number; progress: number; force?: boolean; ended?: boolean }) => void;
}

interface PlayerState {
  isPlaying: boolean;
  isBuffering: boolean;
  playbackError: string | null;
  currentTime: number;
  duration: number;
  progress: number;
  buffered: number;
  volume: number;
  isMuted: boolean;
  previousVolume: number;
  isFullscreen: boolean;
  playbackRate: number;
  quality: string;
  showControls: boolean;
  showSettings: boolean;
  isLocked: boolean;
  isPip: boolean;
  showStats: boolean;
  isIdle: boolean;
  subtitlesEnabled: boolean;
  actionFeedback: { icon: React.ReactNode; id: number; text?: string } | null;
}

type PlayerAction =
  | { type: 'SET_PLAYING'; payload: boolean }
  | { type: 'SET_BUFFERING'; payload: boolean }
  | { type: 'SET_ERROR'; payload: string | null }
  | { type: 'SET_TIME'; payload: { current: number; duration: number; progress: number; buffered: number } }
  | { type: 'SET_VOLUME'; payload: number }
  | { type: 'TOGGLE_MUTE' }
  | { type: 'SET_FULLSCREEN'; payload: boolean }
  | { type: 'SET_PLAYBACK_RATE'; payload: number }
  | { type: 'SET_QUALITY'; payload: string }
  | { type: 'SET_SHOW_CONTROLS'; payload: boolean }
  | { type: 'TOGGLE_SETTINGS' }
  | { type: 'TOGGLE_LOCK' }
  | { type: 'SET_PIP'; payload: boolean }
  | { type: 'TOGGLE_STATS' }
  | { type: 'SET_IDLE'; payload: boolean }
  | { type: 'SET_SUBTITLES'; payload: boolean }
  | { type: 'SET_FEEDBACK'; payload: { icon: React.ReactNode; id: number; text?: string } | null }
  | { type: 'RESET' };

const SKIP_SECONDS = 10;
const DOUBLE_TAP_DELAY = 300;
const CONTROLS_HIDE_DELAY = 3000;
const IDLE_TIMEOUT = 3000;
const FEEDBACK_DURATION = 800;
const PLAYBACK_RATES = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
const QUALITY_OPTIONS = [
  { label: 'Full HD', value: '1080p' },
  { label: 'HD', value: '720p' },
  { label: 'SD', value: '480p' },
  { label: 'Auto', value: 'auto' },
];

const formatTime = (seconds: number): string => {
  if (!seconds || isNaN(seconds)) return '0:00';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
};

const getStoredPreference = <T,>(key: string, defaultValue: T): T => {
  try {
    const stored = localStorage.getItem(`v-pro-player-${key}`);
    return stored ? (JSON.parse(stored) as T) : defaultValue;
  } catch {
    return defaultValue;
  }
};

const setStoredPreference = (key: string, value: unknown) => {
  try {
    localStorage.setItem(`v-pro-player-${key}`, JSON.stringify(value));
  } catch {
    // ignore
  }
};

const playerReducer = (state: PlayerState, action: PlayerAction): PlayerState => {
  switch (action.type) {
    case 'SET_PLAYING':
      return { ...state, isPlaying: action.payload };
    case 'SET_BUFFERING':
      return { ...state, isBuffering: action.payload };
    case 'SET_ERROR':
      return { ...state, playbackError: action.payload };
    case 'SET_TIME':
      return {
        ...state,
        currentTime: action.payload.current,
        duration: action.payload.duration,
        progress: action.payload.progress,
        buffered: action.payload.buffered,
      };
    case 'SET_VOLUME': {
      const newVolume = Math.max(0, Math.min(1, action.payload));
      setStoredPreference('volume', newVolume);
      return {
        ...state,
        volume: newVolume,
        isMuted: newVolume === 0,
        previousVolume: newVolume > 0 ? newVolume : state.previousVolume,
      };
    }
    case 'TOGGLE_MUTE': {
      if (state.isMuted) {
        const restored = state.previousVolume || 0.8;
        return { ...state, isMuted: false, volume: restored, previousVolume: restored };
      }
      return { ...state, isMuted: true, volume: 0, previousVolume: state.volume || state.previousVolume || 0.8 };
    }
    case 'SET_FULLSCREEN':
      return { ...state, isFullscreen: action.payload };
    case 'SET_PLAYBACK_RATE':
      setStoredPreference('playback-rate', action.payload);
      return { ...state, playbackRate: action.payload };
    case 'SET_QUALITY':
      setStoredPreference('quality', action.payload);
      return { ...state, quality: action.payload };
    case 'SET_SHOW_CONTROLS':
      return { ...state, showControls: action.payload };
    case 'TOGGLE_SETTINGS':
      return { ...state, showSettings: !state.showSettings, showControls: true, isIdle: false };
    case 'TOGGLE_LOCK':
      return { ...state, isLocked: !state.isLocked, showControls: true, isIdle: false };
    case 'SET_PIP':
      return { ...state, isPip: action.payload };
    case 'TOGGLE_STATS':
      return { ...state, showStats: !state.showStats };
    case 'SET_IDLE':
      return { ...state, isIdle: action.payload };
    case 'SET_SUBTITLES':
      setStoredPreference('subtitles', action.payload);
      return { ...state, subtitlesEnabled: action.payload };
    case 'SET_FEEDBACK':
      return { ...state, actionFeedback: action.payload };
    case 'RESET':
      return {
        ...state,
        isPlaying: false,
        isBuffering: true,
        playbackError: null,
        currentTime: 0,
        duration: 0,
        progress: 0,
        buffered: 0,
        isIdle: false,
        actionFeedback: null,
      };
    default:
      return state;
  }
};

const ProgressBar: React.FC<{
  progress: number;
  buffered: number;
  currentTime: number;
  duration: number;
  onSeek: (value: number) => void;
  compact?: boolean;
}> = ({ progress, buffered, currentTime, duration, onSeek, compact = false }) => {
  const [isHovering, setIsHovering] = useState(false);
  const [hoverX, setHoverX] = useState(0);
  const [hoverTime, setHoverTime] = useState(0);
  const barRef = useRef<HTMLDivElement>(null);

  const selectAtClientX = (clientX: number) => {
    const rect = barRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return;
    const x = Math.max(0, Math.min(clientX - rect.left, rect.width));
    onSeek((x / rect.width) * 100);
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    if (!barRef.current) return;
    const rect = barRef.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
    const percent = (x / rect.width) * 100;
    setHoverX(percent);
    setHoverTime((duration / 100) * percent);
  };

  const rightLabel = compact ? formatTime(duration) : `-${formatTime(Math.max(0, duration - currentTime))}`;

  return (
    <div className={`flex items-center ${compact ? 'gap-2 mb-3' : 'gap-4 mb-4'} group/timeline select-none`}>
      <span className={`font-medium text-slate-300 text-right font-mono ${compact ? 'text-[11px] w-10' : 'text-xs w-12'}`}>{formatTime(currentTime)}</span>

      <div
        ref={barRef}
        className={`relative flex-1 bg-white/10 rounded-full cursor-pointer transition-all duration-200 group/bar ${compact ? 'h-2 hover:h-2.5' : 'h-1.5 hover:h-2.5'}`}
        onMouseEnter={() => setIsHovering(true)}
        onMouseLeave={() => setIsHovering(false)}
        onMouseMove={handleMouseMove}
        onClick={(e) => selectAtClientX(e.clientX)}
        onTouchStart={(e) => {
          const touch = e.touches[0];
          if (touch) selectAtClientX(touch.clientX);
        }}
        onTouchMove={(e) => {
          const touch = e.touches[0];
          if (touch) selectAtClientX(touch.clientX);
        }}
      >
        <div className="absolute -inset-y-3 inset-x-0 z-10" />

        {!compact && isHovering ? (
          <>
            <div
              className="absolute bottom-full mb-3 px-2 py-1 bg-slate-900 border border-white/10 rounded text-[10px] font-mono text-white whitespace-nowrap -translate-x-1/2 pointer-events-none z-50 shadow-xl"
              style={{ left: `${hoverX}%` }}
            >
              {formatTime(hoverTime)}
            </div>
            <div className="absolute h-full bg-white/20 rounded-full pointer-events-none z-10" style={{ width: `${hoverX}%` }} />
          </>
        ) : null}

        <div className="absolute inset-0 rounded-full bg-white/10" aria-hidden="true" />

        <div
          className="absolute top-0 left-0 h-full bg-white/30 rounded-full transition-all duration-300 z-0"
          style={{ width: `${buffered}%` }}
          aria-hidden="true"
        />

        <div
          className="absolute top-0 left-0 h-full bg-gradient-to-r from-indigo-600 to-indigo-400 rounded-full z-20 shadow-[0_0_10px_rgba(79,70,229,0.5)]"
          style={{ width: `${progress}%` }}
          aria-hidden="true"
        />

        <div
          className={`absolute bg-white rounded-full shadow-lg transition-transform duration-150 pointer-events-none z-30 ring-4 ring-indigo-500/30 ${compact ? 'h-3.5 w-3.5' : 'h-4 w-4 scale-0 group-hover/bar:scale-100'}`}
          style={{ left: `${progress}%`, transform: 'translateX(-50%)' }}
          aria-hidden="true"
        >
          <div className="absolute inset-0 rounded-full bg-indigo-500 opacity-20 animate-pulse" />
        </div>
      </div>

      <span className={`font-medium text-slate-300 font-mono ${compact ? 'text-[11px] w-10 text-right' : 'text-xs w-12'}`}>{rightLabel}</span>
    </div>
  );
};

const VolumeControl: React.FC<{
  volume: number;
  isMuted: boolean;
  onVolumeChange: (value: number) => void;
  onMuteToggle: () => void;
  compact?: boolean;
}> = ({ volume, isMuted, onVolumeChange, onMuteToggle, compact = false }) => {
  const iconSize = compact ? 20 : 22;
  const volumeIcon = useMemo(() => {
    if (isMuted || volume === 0) return <VolumeX size={iconSize} />;
    if (volume < 0.5) return <Volume1 size={iconSize} />;
    return <Volume2 size={iconSize} />;
  }, [iconSize, isMuted, volume]);

  return (
    <div className="flex items-center group/volume relative">
      <button
        onClick={onMuteToggle}
        className="p-2 text-slate-300 hover:text-white transition rounded-full hover:bg-white/10"
        title={isMuted ? 'Desmutear' : 'Mutear'}
        aria-label={isMuted ? 'Desmutear' : 'Mutear'}
      >
        {volumeIcon}
      </button>
      {!compact ? (
        <div className="w-0 overflow-hidden group-hover/volume:w-24 transition-all duration-300 ease-out flex items-center ml-1">
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={isMuted ? 0 : volume}
            onChange={(e) => onVolumeChange(parseFloat(e.target.value))}
            className="h-1 w-20 bg-white/30 rounded-lg appearance-none cursor-pointer accent-indigo-500 hover:accent-white"
            aria-label="Volumen"
          />
        </div>
      ) : null}
    </div>
  );
};

const SettingsMenu: React.FC<{
  playbackRate: number;
  quality: string;
  showStats: boolean;
  brightness: number;
  contrast: number;
  onPlaybackRateChange: (rate: number) => void;
  onQualityChange: (quality: string) => void;
  onToggleStats: () => void;
  onBrightnessChange: (val: number) => void;
  onContrastChange: (val: number) => void;
  onClose: () => void;
  compact?: boolean;
}> = ({
  playbackRate,
  quality,
  showStats,
  brightness,
  contrast,
  onPlaybackRateChange,
  onQualityChange,
  onToggleStats,
  onBrightnessChange,
  onContrastChange,
  onClose,
  compact = false,
}) => {
  const [view, setView] = useState<'main' | 'speed' | 'display'>('main');
  const { t } = useI18n();

  return (
    <div className={`absolute bottom-full right-0 ${compact ? "mb-2 w-[min(88vw,20rem)]" : "mb-4 w-72"} bg-slate-950/95 backdrop-blur-xl border border-white/10 rounded-2xl overflow-hidden shadow-2xl animate-in zoom-in-95 duration-200 z-50`}>
      {view === 'main' ? (
        <div className="p-2">
          <button
            onClick={() => setView('speed')}
            className="w-full flex items-center justify-between p-3 hover:bg-white/10 rounded-xl transition text-sm text-white"
          >
            <div className="flex items-center gap-3">
              <Clock size={18} className="text-indigo-400" />
              <span>{t('player_speed')}</span>
            </div>
            <span className="text-indigo-400 font-bold">{playbackRate}x</span>
          </button>

          <button
            onClick={() => setView('display')}
            className="w-full flex items-center justify-between p-3 hover:bg-white/10 rounded-xl transition text-sm text-white"
          >
            <div className="flex items-center gap-3">
              <Settings size={18} className="text-indigo-400" />
              <span>{t('display_settings') || 'Ajustes de Imagen'}</span>
            </div>
            <ChevronRight size={16} className="text-slate-500" />
          </button>

          <button
            onClick={onToggleStats}
            className="w-full flex items-center justify-between p-3 hover:bg-white/10 rounded-xl transition text-sm text-white"
          >
            <div className="flex items-center gap-3">
              <Info size={18} className="text-indigo-400" />
              <span>{t('player_stats')}</span>
            </div>
            <div className={`w-8 h-4 rounded-full relative transition ${showStats ? 'bg-indigo-600' : 'bg-white/20'}`}>
              <div className={`absolute top-1 w-2 h-2 bg-white rounded-full transition-all ${showStats ? 'left-5' : 'left-1'}`} />
            </div>
          </button>
          <button
            onClick={onClose}
            className="w-full mt-1 flex items-center justify-center gap-2 p-2 text-xs font-bold uppercase text-slate-400 hover:text-white hover:bg-white/5 rounded-xl transition"
          >
            <X size={14} /> {t('close')}
          </button>
        </div>
      ) : null}

      {view === 'speed' ? (
        <div className="p-2">
          <button
            onClick={() => setView('main')}
            className="w-full flex items-center gap-2 p-3 border-b border-white/5 mb-1 text-slate-400 hover:text-white transition"
          >
            <ChevronLeft size={18} /> <span className="text-xs font-bold uppercase">{t('back')}</span>
          </button>
          <div className="grid grid-cols-2 gap-1">
            {PLAYBACK_RATES.map((rate) => (
              <button
                key={rate}
                onClick={() => {
                  onPlaybackRateChange(rate);
                  setView('main');
                }}
                className={`p-2 rounded-lg text-sm transition ${
                  playbackRate === rate ? 'bg-indigo-600 text-white' : 'hover:bg-white/5 text-slate-200'
                }`}
                aria-pressed={playbackRate === rate}
              >
                {rate === 1 ? t('normal') : `${rate}x`}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {view === 'display' ? (
        <div className="p-4 space-y-4">
          <button
            onClick={() => setView('main')}
            className="w-full flex items-center gap-2 pb-2 border-b border-white/5 mb-2 text-slate-400 hover:text-white transition"
          >
            <ChevronLeft size={18} /> <span className="text-xs font-bold uppercase">{t('back')}</span>
          </button>
          
          <div className="space-y-2">
            <div className="flex justify-between text-xs text-slate-400 uppercase font-bold">
              <span>{t('brightness') || 'Brillo'}</span>
              <span className="text-indigo-400">{brightness}%</span>
            </div>
            <input
              type="range"
              min="50"
              max="150"
              value={brightness}
              onChange={(e) => onBrightnessChange(parseInt(e.target.value))}
              className="w-full h-1 bg-white/10 rounded-lg appearance-none cursor-pointer accent-indigo-500"
            />
          </div>

          <div className="space-y-2">
            <div className="flex justify-between text-xs text-slate-400 uppercase font-bold">
              <span>{t('contrast') || 'Contraste'}</span>
              <span className="text-indigo-400">{contrast}%</span>
            </div>
            <input
              type="range"
              min="50"
              max="150"
              value={contrast}
              onChange={(e) => onContrastChange(parseInt(e.target.value))}
              className="w-full h-1 bg-white/10 rounded-lg appearance-none cursor-pointer accent-indigo-500"
            />
          </div>

          <button
            onClick={() => {
              onBrightnessChange(100);
              onContrastChange(100);
            }}
            className="w-full py-2 text-xs font-bold text-indigo-400 hover:text-indigo-300 transition"
          >
            {t('reset_defaults') || 'Restablecer valores'}
          </button>
        </div>
      ) : null}
    </div>
  );
};

const ControlButtons: React.FC<{
  isPlaying: boolean;
  onPlayToggle: () => void;
  onSkip: (seconds: number) => void;
  onPrevItem?: () => void;
  onNextItem?: () => void;
  hasPrevItem?: boolean;
  hasNextItem?: boolean;
  volume: number;
  isMuted: boolean;
  onVolumeChange: (value: number) => void;
  onMuteToggle: () => void;
  playbackRate: number;
  quality: string;
  brightness: number;
  contrast: number;
  onPlaybackRateChange: (rate: number) => void;
  onQualityChange: (quality: string) => void;
  onBrightnessChange: (val: number) => void;
  onContrastChange: (val: number) => void;
  showSettings: boolean;
  onSettingsToggle: () => void;
  subtitlesAvailable: boolean;
  subtitlesEnabled: boolean;
  onSubtitlesToggle: () => void;
  episodesAvailable: boolean;
  episodesOpen: boolean;
  onEpisodesToggle: () => void;
  isPip: boolean;
  onPipToggle: () => void;
  showStats: boolean;
  onStatsToggle: () => void;
  isFullscreen: boolean;
  onFullscreenToggle: () => void;
  isLocked: boolean;
  onLockToggle: () => void;
  compact?: boolean;
}> = ({
  isPlaying,
  onPlayToggle,
  onSkip,
  onPrevItem,
  onNextItem,
  hasPrevItem,
  hasNextItem,
  volume,
  isMuted,
  onVolumeChange,
  onMuteToggle,
  playbackRate,
  quality,
  brightness,
  contrast,
  onPlaybackRateChange,
  onQualityChange,
  onBrightnessChange,
  onContrastChange,
  showSettings,
  onSettingsToggle,
  subtitlesAvailable,
  subtitlesEnabled,
  onSubtitlesToggle,
  episodesAvailable,
  episodesOpen,
  onEpisodesToggle,
  isPip,
  onPipToggle,
  showStats,
  onStatsToggle,
  isFullscreen,
  onFullscreenToggle,
  isLocked,
  onLockToggle,
  compact = false,
}) => {
  const { t } = useI18n();
  const rewindLabel = t('rewind_seconds').replace('{seconds}', String(SKIP_SECONDS));
  const forwardLabel = t('forward_seconds').replace('{seconds}', String(SKIP_SECONDS));
  const iconSize = compact ? 20 : 22;
  const navIconSize = compact ? 18 : 20;
  const playSize = compact ? 30 : 32;

  return (
    <div className={`flex ${compact ? 'flex-col gap-2' : 'items-center justify-between'}`}>
      <div className={`flex items-center ${compact ? 'w-full justify-between gap-2' : 'gap-4 md:gap-6'}`}>
        {!compact ? (
          <button
            onClick={onLockToggle}
            className={`p-2 transition rounded-full ${isLocked ? 'text-indigo-400 bg-white/10' : 'text-slate-300 hover:text-white hover:bg-white/10'}`}
            aria-label={isLocked ? t('unlock') : t('lock')}
            title={isLocked ? t('unlock') : t('lock')}
          >
            {isLocked ? <Lock size={20} /> : <Unlock size={20} />}
          </button>
        ) : null}

        {!isLocked ? (
          <>
            <button
              onClick={onPlayToggle}
              className="text-white hover:text-indigo-400 transition-all hover:scale-110 active:scale-95"
              aria-label={isPlaying ? t('pause') : t('play')}
              title={isPlaying ? t('pause') : t('play')}
            >
              {isPlaying ? <Pause size={playSize} fill="currentColor" /> : <Play size={playSize} fill="currentColor" />}
            </button>

            <div className={`flex items-center ${compact ? 'gap-1.5' : 'gap-2'}`}>
              {!compact && onPrevItem ? (
                <button
                  onClick={onPrevItem}
                  disabled={!hasPrevItem}
                  className="p-2 text-slate-400 hover:text-white disabled:opacity-30 transition hover:bg-white/10 rounded-full"
                  aria-label={t('previous_video')}
                  title={t('previous_video')}
                >
                  <ChevronLeft size={navIconSize} />
                </button>
              ) : null}
              <button
                onClick={() => onSkip(-SKIP_SECONDS)}
                className="p-2 text-slate-300 hover:text-white transition hover:bg-white/10 rounded-full"
                aria-label={rewindLabel}
                title={rewindLabel}
              >
                <SkipBack size={navIconSize} />
              </button>
              <button
                onClick={() => onSkip(SKIP_SECONDS)}
                className="p-2 text-slate-300 hover:text-white transition hover:bg-white/10 rounded-full"
                aria-label={forwardLabel}
                title={forwardLabel}
              >
                <SkipForward size={navIconSize} />
              </button>
              {!compact && onNextItem ? (
                <button
                  onClick={onNextItem}
                  disabled={!hasNextItem}
                  className="p-2 text-slate-400 hover:text-white disabled:opacity-30 transition hover:bg-white/10 rounded-full"
                  aria-label={t('next_video')}
                  title={t('next_video')}
                >
                  <ChevronRight size={navIconSize} />
                </button>
              ) : null}
            </div>

            <VolumeControl volume={volume} isMuted={isMuted} onVolumeChange={onVolumeChange} onMuteToggle={onMuteToggle} compact={compact} />
          </>
        ) : null}
      </div>

      {!isLocked ? (
        <div className={`flex items-center ${compact ? 'w-full justify-between gap-1.5' : 'gap-3'}`}>
          <button
            onClick={onSubtitlesToggle}
            disabled={!subtitlesAvailable}
            className={`p-2 transition rounded-full ${
              subtitlesAvailable ? 'hover:bg-white/10' : 'opacity-40 cursor-not-allowed'
            } ${subtitlesEnabled ? 'text-white bg-white/10' : 'text-slate-300 hover:text-white'}`}
            aria-label={t('subtitles')}
            title={t('subtitles')}
          >
            <Subtitles size={iconSize} />
          </button>

          <button
            onClick={onEpisodesToggle}
            disabled={!episodesAvailable}
            className={`p-2 transition rounded-full ${
              episodesAvailable ? 'hover:bg-white/10' : 'opacity-40 cursor-not-allowed'
            } ${episodesOpen ? 'text-white bg-white/10' : 'text-slate-300 hover:text-white'}`}
            aria-label={t('episodes')}
            title={t('episodes_shortcut')}
          >
            <List size={iconSize} />
          </button>

          {!compact ? (
            <button
              onClick={onPipToggle}
              className={`p-2 transition rounded-full hover:bg-white/10 ${isPip ? 'text-white bg-white/10' : 'text-slate-300 hover:text-white'}`}
              aria-label={t('pip')}
              title={t('pip')}
            >
              <PictureInPicture size={iconSize} />
            </button>
          ) : null}

          <div className="relative">
            {showSettings ? (
              <SettingsMenu
                playbackRate={playbackRate}
                quality={quality}
                showStats={showStats}
                brightness={brightness}
                contrast={contrast}
                onPlaybackRateChange={onPlaybackRateChange}
                onQualityChange={onQualityChange}
                onToggleStats={onStatsToggle}
                onBrightnessChange={onBrightnessChange}
                onContrastChange={onContrastChange}
                onClose={onSettingsToggle}
                compact={compact}
              />
            ) : null}
            <button
              onClick={onSettingsToggle}
              className={`p-2 transition rounded-full hover:bg-white/10 ${showSettings ? 'text-white bg-white/10 rotate-45' : 'text-slate-300'}`}
              aria-label={t('settings')}
              title={t('settings')}
              aria-pressed={showSettings}
            >
              <Settings size={iconSize} />
            </button>
          </div>

          <button
            onClick={onFullscreenToggle}
            className="p-2 text-slate-300 hover:text-white transition hover:bg-white/10 rounded-full"
            aria-label={isFullscreen ? t('exit_fullscreen') : t('fullscreen')}
            title={isFullscreen ? t('exit_fullscreen') : t('fullscreen')}
          >
            {isFullscreen ? <Minimize size={iconSize} /> : <Maximize size={iconSize} />}
          </button>
        </div>
      ) : null}
    </div>
  );
};

export const VideoPlayer: React.FC<VideoPlayerProps> = ({
  src,
  title,
  onClose,
  onPrevItem,
  onNextItem,
  hasPrevItem,
  hasNextItem,
  nowPlayingLabel,
  nextSeriesTitle,
  onContinueNextSeries,
  episodeSeasons,
  currentPath,
  onSelectEpisode,
  poster,
  subtitlesUrl,
  autoPlay = true,
  startTime,
  onProgressUpdate,
}) => {
  const { t } = useI18n();
  const videoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const controlsTimeoutRef = useRef<number | null>(null);
  const feedbackTimeoutRef = useRef<number | null>(null);
  const mouseMoveTimeoutRef = useRef<number | null>(null);
  const clickTimerRef = useRef<number | null>(null);
  const startTimeAppliedRef = useRef(false);
  const lastProgressEmitRef = useRef<number>(0);
  const subtitlesTrackRef = useRef<HTMLTrackElement>(null);
  const userCollapsedAllSeasonsRef = useRef(false);
  const [showContinueOverlay, setShowContinueOverlay] = useState(false);
  const [openSeasonKey, setOpenSeasonKey] = useState<string | null>(null);
  const [episodesPanelOpen, setEpisodesPanelOpen] = useState(false);
  const [brightness, setBrightness] = useState(100);
  const [contrast, setContrast] = useState(100);
  const [isCompact, setIsCompact] = useState<boolean>(() => {
    try {
      return typeof window !== 'undefined' ? (window.innerWidth < 900 || window.innerHeight < 620) : false;
    } catch {
      return false;
    }
  });

  const [state, dispatch] = useReducer(playerReducer, null, () => {
    const storedVol = getStoredPreference('volume', 0.8);
    const initVol = typeof storedVol === 'number' && Number.isFinite(storedVol) ? Math.max(0, Math.min(1, storedVol)) : 0.8;
    const storedSubtitles = getStoredPreference('subtitles', true);
    const initSubtitles = typeof storedSubtitles === 'boolean' ? storedSubtitles : true;
    return {
      isPlaying: false,
      isBuffering: false,
      playbackError: null,
      currentTime: 0,
      duration: 0,
      progress: 0,
      buffered: 0,
      volume: initVol,
      isMuted: initVol === 0,
      previousVolume: initVol > 0 ? initVol : 0.8,
      isFullscreen: false,
      playbackRate: getStoredPreference('playback-rate', 1),
      quality: getStoredPreference('quality', '1080p'),
      showControls: true,
      showSettings: false,
      isLocked: false,
      isPip: false,
      showStats: false,
      isIdle: false,
      subtitlesEnabled: initSubtitles,
      actionFeedback: null,
    } as PlayerState;
  });

  const triggerFeedback = useCallback((icon: React.ReactNode, text?: string) => {
    const id = Date.now();
    dispatch({ type: 'SET_FEEDBACK', payload: { icon, id, text } });

    if (feedbackTimeoutRef.current) window.clearTimeout(feedbackTimeoutRef.current);
    feedbackTimeoutRef.current = window.setTimeout(() => {
      dispatch({ type: 'SET_FEEDBACK', payload: null });
    }, FEEDBACK_DURATION);
  }, []);

  const emitProgress = useCallback(
    (force = false, ended = false) => {
      if (!onProgressUpdate) return;
      const v = videoRef.current;
      if (!v) return;
      const dur = v.duration;
      if (!Number.isFinite(dur) || dur <= 0) return;
      const current = v.currentTime;
      const progress = (current / dur) * 100;
      const now = Date.now();
      if (!force && now - lastProgressEmitRef.current < 5000) return;
      lastProgressEmitRef.current = now;
      onProgressUpdate({ current, duration: dur, progress, force, ended });
    },
    [onProgressUpdate],
  );

  const handleClose = useCallback(() => {
    try {
      if (document.fullscreenElement) void document.exitFullscreen();
    } catch {
      // ignore
    }
    onClose();
  }, [onClose]);

  const togglePlay = useCallback(() => {
    if (state.isLocked) return;
    const v = videoRef.current;
    if (!v) return;

    dispatch({ type: 'SET_ERROR', payload: null });

    if (v.paused || v.ended) {
      v.play().catch((e) => {
        console.error('Play failed', e);
        dispatch({ type: 'SET_ERROR', payload: t('playback_failed') });
      });
      triggerFeedback(<Play size={48} className="fill-white text-white drop-shadow-lg" />, t('play'));
    } else {
      v.pause();
      triggerFeedback(<Pause size={48} className="fill-white text-white drop-shadow-lg" />, t('pause'));
    }
  }, [state.isLocked, t, triggerFeedback]);

  const toggleMute = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;

    const nextMuted = !state.isMuted;
    dispatch({ type: 'TOGGLE_MUTE' });
    v.muted = nextMuted;

    triggerFeedback(nextMuted ? <VolumeX size={48} /> : <Volume2 size={48} />, nextMuted ? t('mute') : t('audio'));
  }, [state.isMuted, t, triggerFeedback]);

  const toggleFullscreen = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;

    try {
      if (document.fullscreenElement) void document.exitFullscreen();
      else void el.requestFullscreen();
    } catch {
      // ignore
    }
  }, []);

  const togglePip = useCallback(async () => {
    if (state.isLocked) return;
    const v = videoRef.current;
    if (!v) return;

    const docAny = document as Document & {
      pictureInPictureElement?: Element | null;
      exitPictureInPicture?: () => Promise<void>;
    };
    const vAny = v as HTMLVideoElement & {
      requestPictureInPicture?: () => Promise<unknown>;
    };

    try {
      if (docAny.pictureInPictureElement && docAny.exitPictureInPicture) {
        await docAny.exitPictureInPicture();
        dispatch({ type: 'SET_PIP', payload: false });
        triggerFeedback(<PictureInPicture size={48} />, t('pip'));
        return;
      }
      if (vAny.requestPictureInPicture) {
        await vAny.requestPictureInPicture();
        dispatch({ type: 'SET_PIP', payload: true });
        triggerFeedback(<PictureInPicture size={48} />, t('pip'));
      }
    } catch (e) {
      console.error('PiP error', e);
    }
  }, [state.isLocked, t, triggerFeedback]);

  const toggleSubtitles = useCallback(() => {
    if (!subtitlesUrl) return;
    const next = !state.subtitlesEnabled;
    dispatch({ type: 'SET_SUBTITLES', payload: next });
    triggerFeedback(<Subtitles size={48} />, next ? t('subtitles') : t('subtitles_off'));
  }, [state.subtitlesEnabled, subtitlesUrl, t, triggerFeedback]);

  const skip = useCallback(
    (seconds: number) => {
      if (state.isLocked) return;
      const v = videoRef.current;
      if (!v) return;

      const dur = Number.isFinite(v.duration) ? v.duration : 0;
      const next = v.currentTime + seconds;
      v.currentTime = Math.max(0, Math.min(dur || Number.MAX_SAFE_INTEGER, next));

      if (seconds > 0) {
        triggerFeedback(
          <div className="flex flex-col items-center">
            <FastForward size={48} />
            <span className="text-sm font-bold mt-1">+{Math.abs(seconds)}s</span>
          </div>
        );
      } else {
        triggerFeedback(
          <div className="flex flex-col items-center">
            <Rewind size={48} />
            <span className="text-sm font-bold mt-1">-{Math.abs(seconds)}s</span>
          </div>
        );
      }
    },
    [state.isLocked, triggerFeedback]
  );

  const handleSeek = useCallback(
    (value: number) => {
      if (state.isLocked) return;
      const v = videoRef.current;
      if (!v) return;
      const dur = v.duration;
      if (!Number.isFinite(dur) || dur <= 0) return;

      const seekTime = (dur / 100) * value;
      v.currentTime = seekTime;
      dispatch({
        type: 'SET_TIME',
        payload: { current: seekTime, duration: dur, progress: value, buffered: state.buffered },
      });
    },
    [state.buffered, state.isLocked]
  );

  const handleVolumeChange = useCallback((val: number) => {
    dispatch({ type: 'SET_VOLUME', payload: val });

    const v = videoRef.current;
    if (v) {
      v.volume = Math.max(0, Math.min(1, val));
      v.muted = val === 0;
    }
  }, []);

  const handlePlaybackRateChange = useCallback((rate: number) => {
    dispatch({ type: 'SET_PLAYBACK_RATE', payload: rate });
    const v = videoRef.current;
    if (v) v.playbackRate = rate;
  }, []);

  const handleQualityChange = useCallback((quality: string) => {
    dispatch({ type: 'SET_QUALITY', payload: quality });
  }, []);

  const handleRetry = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;

    dispatch({ type: 'SET_ERROR', payload: null });
    dispatch({ type: 'SET_BUFFERING', payload: true });
    v.load();
    v.play().catch((e) => dispatch({ type: 'SET_ERROR', payload: String(e) }));
  }, []);

  const handleEnded = useCallback(() => {
    emitProgress(true, true);
    dispatch({ type: 'SET_PLAYING', payload: false });
    dispatch({ type: 'SET_SHOW_CONTROLS', payload: true });
    if (onNextItem && hasNextItem !== false) {
      try {
        triggerFeedback(<SkipForward size={48} />, t('next'));
      } catch {
        // ignore
      }
      // Small UI delay before switching item.
      window.setTimeout(() => {
        try {
          onNextItem();
        } catch {
          // ignore
        }
      }, 250);
      return;
    }
    if (onContinueNextSeries && nextSeriesTitle) {
      setShowContinueOverlay(true);
    }
  }, [emitProgress, onNextItem, hasNextItem, t, triggerFeedback, onContinueNextSeries, nextSeriesTitle]);

  const handleTimeUpdate = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;

    const current = v.currentTime;
    const dur = v.duration;
    if (!Number.isFinite(dur) || dur <= 0) return;

    const newProgress = (current / dur) * 100;

    let newBuffered = state.buffered;
    try {
      if (v.buffered.length > 0) {
        for (let i = 0; i < v.buffered.length; i++) {
          if (v.buffered.start(i) <= current && v.buffered.end(i) >= current) {
            newBuffered = (v.buffered.end(i) / dur) * 100;
            break;
          }
        }
      }
    } catch {
      // ignore
    }

    if (state.isBuffering && !v.paused && v.readyState >= 2) {
      dispatch({ type: 'SET_BUFFERING', payload: false });
    }

    dispatch({ type: 'SET_TIME', payload: { current, duration: dur, progress: newProgress, buffered: newBuffered } });
    emitProgress(false, false);
  }, [emitProgress, state.buffered, state.isBuffering]);

  const applySubtitlesMode = useCallback(
    (enabled: boolean) => {
      try {
        const v = videoRef.current;
        if (!v) return;
        const tt = v.textTracks?.[0];
        if (!tt) return;
        tt.mode = enabled ? 'showing' : 'disabled';
      } catch {
        // ignore
      }
    },
    []
  );

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (document.activeElement?.tagName === 'INPUT') return;

      if (e.key.toLowerCase() === 'l' && e.shiftKey) {
        e.preventDefault();
        dispatch({ type: 'TOGGLE_LOCK' });
        return;
      }

      if (state.isLocked) return;

      switch (e.key) {
        case 'Escape':
          if (state.showSettings) {
            e.preventDefault();
            dispatch({ type: 'TOGGLE_SETTINGS' });
            return;
          }
          if (!document.fullscreenElement) {
            e.preventDefault();
            handleClose();
          }
          break;
        case 'i':
          e.preventDefault();
          dispatch({ type: 'TOGGLE_STATS' });
          break;
        case ' ':
        case 'k':
          e.preventDefault();
          togglePlay();
          break;
        case 'ArrowRight':
        case 'l':
          e.preventDefault();
          skip(SKIP_SECONDS);
          break;
        case 'ArrowLeft':
        case 'j':
          e.preventDefault();
          skip(-SKIP_SECONDS);
          break;
        case 'm':
          e.preventDefault();
          toggleMute();
          break;
        case 'p':
          e.preventDefault();
          void togglePip();
          break;
        case 's':
          e.preventDefault();
          toggleSubtitles();
          break;
        case 'e':
        case 'E':
          e.preventDefault();
          if (!Array.isArray(episodeSeasons) || episodeSeasons.length === 0) break;
          dispatch({ type: 'SET_SHOW_CONTROLS', payload: true });
          dispatch({ type: 'SET_IDLE', payload: false });
          setEpisodesPanelOpen((v) => {
            const next = !v;
            if (next) userCollapsedAllSeasonsRef.current = false;
            return next;
          });
          break;
        case 'f':
          e.preventDefault();
          toggleFullscreen();
          break;
        case 'ArrowUp':
          e.preventDefault();
          handleVolumeChange(Math.min(1, state.volume + 0.1));
          break;
        case 'ArrowDown':
          e.preventDefault();
          handleVolumeChange(Math.max(0, state.volume - 0.1));
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [
    handleClose,
    handleVolumeChange,
    skip,
    state.showSettings,
    state.volume,
    toggleSubtitles,
    toggleFullscreen,
    toggleMute,
    togglePip,
    togglePlay,
    episodeSeasons,
  ]);

  useEffect(() => {
    const onFs = () => dispatch({ type: 'SET_FULLSCREEN', payload: !!document.fullscreenElement });
    document.addEventListener('fullscreenchange', onFs);
    return () => document.removeEventListener('fullscreenchange', onFs);
  }, []);

  useEffect(() => {
    dispatch({ type: 'RESET' });
    startTimeAppliedRef.current = false;
    setShowContinueOverlay(false);
    setEpisodesPanelOpen(false);
    userCollapsedAllSeasonsRef.current = false;
    if (Array.isArray(episodeSeasons) && episodeSeasons.length) {
      setOpenSeasonKey((prev) => prev && episodeSeasons.some(s => s.key === prev) ? prev : episodeSeasons[0].key);
    } else {
      setOpenSeasonKey(null);
    }

    const v = videoRef.current;
    if (!v) return;

    if (autoPlay) {
      const playPromise = v.play();
      if (playPromise) playPromise.catch(() => { /* autoplay blocked */ });
    }
  }, [autoPlay, src]);

  useEffect(() => {
    if (!episodesPanelOpen) return;
    if (!Array.isArray(episodeSeasons) || !episodeSeasons.length) return;
    setOpenSeasonKey((prev) => {
      if (prev && episodeSeasons.some((s) => s.key === prev)) return prev;
      if (prev === null && userCollapsedAllSeasonsRef.current) return null;
      return episodeSeasons[0].key;
    });
  }, [episodesPanelOpen, episodeSeasons]);

  useEffect(() => {
    if (controlsTimeoutRef.current) window.clearTimeout(controlsTimeoutRef.current);

    const shouldShowControls = state.showSettings || !!state.playbackError || state.isBuffering || !state.isPlaying;
    if (shouldShowControls) {
      dispatch({ type: 'SET_SHOW_CONTROLS', payload: true });
      dispatch({ type: 'SET_IDLE', payload: false });
      return;
    }

    controlsTimeoutRef.current = window.setTimeout(() => {
      dispatch({ type: 'SET_SHOW_CONTROLS', payload: false });
      dispatch({ type: 'SET_IDLE', payload: true });
    }, CONTROLS_HIDE_DELAY);

    return () => {
      if (controlsTimeoutRef.current) window.clearTimeout(controlsTimeoutRef.current);
    };
  }, [state.showSettings, state.isPlaying, state.isBuffering, state.playbackError]);

  const handleMouseMove = useCallback(() => {
    dispatch({ type: 'SET_SHOW_CONTROLS', payload: true });
    dispatch({ type: 'SET_IDLE', payload: false });

    if (mouseMoveTimeoutRef.current) window.clearTimeout(mouseMoveTimeoutRef.current);

    if (!state.showSettings && !state.playbackError && state.isPlaying) {
      mouseMoveTimeoutRef.current = window.setTimeout(() => {
        dispatch({ type: 'SET_SHOW_CONTROLS', payload: false });
        dispatch({ type: 'SET_IDLE', payload: true });
      }, CONTROLS_HIDE_DELAY);
    }
  }, [state.showSettings, state.playbackError, state.isPlaying]);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;

    v.playbackRate = state.playbackRate;
    v.volume = state.volume;
    v.muted = state.isMuted;
  }, [state.playbackRate, state.volume, state.isMuted]);

  useEffect(() => {
    applySubtitlesMode(state.subtitlesEnabled);
  }, [applySubtitlesMode, state.subtitlesEnabled]);

  const handleContainerClick = useCallback(
    (e: React.MouseEvent) => {
      if (state.playbackError) return;
      if (state.showSettings) {
        dispatch({ type: 'TOGGLE_SETTINGS' });
        return;
      }
      if (state.isLocked) return;

      if (clickTimerRef.current) {
        window.clearTimeout(clickTimerRef.current);
        clickTimerRef.current = null;

        const rect = containerRef.current?.getBoundingClientRect();
        if (!rect) return;
        const x = e.clientX - rect.left;
        if (x < rect.width * 0.3) skip(-SKIP_SECONDS);
        else if (x > rect.width * 0.7) skip(SKIP_SECONDS);
        else toggleFullscreen();
        return;
      }

      clickTimerRef.current = window.setTimeout(() => {
        togglePlay();
        clickTimerRef.current = null;
      }, DOUBLE_TAP_DELAY);
    },
    [skip, state.isLocked, state.playbackError, state.showSettings, toggleFullscreen, togglePlay]
  );

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;

    const onEnter = () => dispatch({ type: 'SET_PIP', payload: true });
    const onLeave = () => dispatch({ type: 'SET_PIP', payload: false });

    v.addEventListener('enterpictureinpicture', onEnter as EventListener);
    v.addEventListener('leavepictureinpicture', onLeave as EventListener);
    return () => {
      v.removeEventListener('enterpictureinpicture', onEnter as EventListener);
      v.removeEventListener('leavepictureinpicture', onLeave as EventListener);
    };
  }, []);

  useEffect(() => {
    return () => {
      if (controlsTimeoutRef.current) window.clearTimeout(controlsTimeoutRef.current);
      if (feedbackTimeoutRef.current) window.clearTimeout(feedbackTimeoutRef.current);
      if (mouseMoveTimeoutRef.current) window.clearTimeout(mouseMoveTimeoutRef.current);
      if (clickTimerRef.current) window.clearTimeout(clickTimerRef.current);
      emitProgress(true, false);
    };
  }, [emitProgress]);

  useEffect(() => {
    const recomputeCompact = () => {
      try {
        setIsCompact(window.innerWidth < 900 || window.innerHeight < 620);
      } catch {
        // ignore
      }
    };
    recomputeCompact();
    window.addEventListener('resize', recomputeCompact);
    return () => window.removeEventListener('resize', recomputeCompact);
  }, []);

  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  return (
    <div
      ref={containerRef}
      onMouseMove={handleMouseMove}
      onTouchStart={handleMouseMove}
      onMouseLeave={() => {
        if (state.isPlaying && !state.showSettings) dispatch({ type: 'SET_SHOW_CONTROLS', payload: false });
      }}
      onClick={handleContainerClick}
      className={`fixed inset-0 z-[200] w-screen h-screen bg-[#05070c] flex flex-col justify-center overflow-hidden font-sans select-none group/player text-white ${
        state.isIdle ? 'cursor-none' : 'cursor-default'
      }`}
      role="region"
      aria-label={t('player_aria')}
    >
      <video
        ref={videoRef}
        src={src}
        poster={poster}
        playsInline
        preload="metadata"
        className="w-full h-full object-contain bg-[#05070c] transition-all duration-300"
        style={{
          filter: `brightness(${brightness}%) contrast(${contrast}%)`
        }}
        onTimeUpdate={handleTimeUpdate}
        onProgress={handleTimeUpdate}
        onLoadStart={() => dispatch({ type: 'SET_BUFFERING', payload: true })}
        onLoadedMetadata={() => {
          const v = videoRef.current;
          if (!v) return;

          const dur = Number.isFinite(v.duration) ? v.duration : 0;
          let current = 0;

          if (!startTimeAppliedRef.current && typeof startTime === 'number' && Number.isFinite(startTime) && startTime > 0) {
            try {
              v.currentTime = Math.min(Math.max(0, startTime), dur > 0 ? dur : startTime);
              current = v.currentTime;
            } catch {
              // ignore
            }
            startTimeAppliedRef.current = true;
          }

          dispatch({
            type: 'SET_TIME',
            payload: { current, duration: dur, progress: dur > 0 ? (current / dur) * 100 : 0, buffered: 0 },
          });

          if (autoPlay) {
            const p = v.play();
            if (p) p.catch(() => { /* autoplay blocked */ });
          }
        }}
        onPlay={() => {
          dispatch({ type: 'SET_PLAYING', payload: true });
          dispatch({ type: 'SET_BUFFERING', payload: false });
          dispatch({ type: 'SET_ERROR', payload: null });
        }}
        onPause={() => {
          dispatch({ type: 'SET_PLAYING', payload: false });
          emitProgress(true, false);
        }}
        onWaiting={() => dispatch({ type: 'SET_BUFFERING', payload: true })}
        onCanPlay={() => dispatch({ type: 'SET_BUFFERING', payload: false })}
        onPlaying={() => dispatch({ type: 'SET_BUFFERING', payload: false })}
        onStalled={() => dispatch({ type: 'SET_BUFFERING', payload: true })}
        onEnded={handleEnded}
        onError={() => {
          dispatch({ type: 'SET_PLAYING', payload: false });
          dispatch({ type: 'SET_BUFFERING', payload: false });
          dispatch({ type: 'SET_ERROR', payload: t('load_video_error') });
        }}
      >
        {subtitlesUrl ? (
          <track
            ref={subtitlesTrackRef}
            kind="subtitles"
            srcLang="es"
            label={t('subtitles')}
            src={subtitlesUrl}
            default={state.subtitlesEnabled}
          />
        ) : null}
      </video>

      <div
        className={`absolute top-0 left-0 right-0 h-40 bg-gradient-to-b from-black/80 via-black/40 to-transparent pointer-events-none transition-opacity duration-500 ${
          state.showControls ? 'opacity-100' : 'opacity-0'
        }`}
        aria-hidden="true"
      />
      <div
        className={`absolute bottom-0 left-0 right-0 h-32 sm:h-48 bg-gradient-to-t from-black/90 via-black/50 to-transparent pointer-events-none transition-opacity duration-500 ${
          state.showControls ? 'opacity-100' : 'opacity-0'
        }`}
        aria-hidden="true"
      />

      {state.actionFeedback && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none z-40" aria-live="polite" aria-atomic="true">
          <div className="bg-black/40 backdrop-blur-md p-6 rounded-full border border-white/10 shadow-2xl scale-110">
            {state.actionFeedback.icon}
            {state.actionFeedback.text ? (
              <div className="mt-2 text-[10px] tracking-widest font-bold uppercase text-slate-200 text-center">{state.actionFeedback.text}</div>
            ) : null}
          </div>
        </div>
      )}

      {state.playbackError && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-6" role="alert">
          <div className="w-full max-w-md bg-slate-900/90 border border-red-500/30 rounded-2xl p-6 shadow-2xl text-center">
            <AlertCircle size={48} className="mx-auto text-red-500 mb-4" aria-hidden="true" />
            <h3 className="text-lg font-bold text-white mb-2">{t('something_went_wrong')}</h3>
            <p className="text-slate-300 text-sm mb-6 break-words">{state.playbackError}</p>
            <div className="flex justify-center gap-3">
              <button onClick={handleClose} className="px-4 py-2 rounded-lg bg-white/10 hover:bg-white/20 transition text-sm">{t('close')}</button>
              <button onClick={handleRetry} className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-medium transition text-sm">{t('retry')}</button>
            </div>
          </div>
        </div>
      )}

      {state.isBuffering && !state.playbackError && (
        <div className="absolute inset-0 z-30 flex items-center justify-center pointer-events-none" aria-live="polite">
          <Loader2 className="w-12 h-12 text-indigo-500 animate-spin drop-shadow-lg" aria-hidden="true" />
        </div>
      )}

      {showContinueOverlay && nextSeriesTitle ? (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-6">
          <div className="w-full max-w-md bg-slate-900/90 border border-white/10 rounded-2xl p-6 shadow-2xl text-center">
            <h3 className="text-lg font-bold text-white mb-2">{t('continue_title')}</h3>
            <p className="text-sm text-slate-300 mb-6">
              {t('continue_prompt_prefix')} <span className="text-white font-semibold">{nextSeriesTitle}</span>{t('continue_prompt_suffix')}
            </p>
            <div className="flex justify-center gap-3">
              <button
                onClick={() => setShowContinueOverlay(false)}
                className="px-4 py-2 rounded-lg bg-white/10 hover:bg-white/20 transition text-sm"
              >
                {t('not_now')}
              </button>
              <button
                onClick={() => {
                  setShowContinueOverlay(false);
                  try {
                    onContinueNextSeries?.();
                  } catch {
                    // ignore
                  }
                }}
                className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-medium transition text-sm"
              >
                {t('continue')}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {state.showStats ? (
        <div className={`absolute ${isCompact ? 'top-14 left-3 text-[9px] p-2.5 rounded-xl' : 'top-20 left-6 text-[10px] p-4 rounded-2xl'} z-[55] bg-black/60 backdrop-blur-md border border-white/10 font-mono text-indigo-300 pointer-events-none`}>
          <div>{t('stats_resolution')}: {videoRef.current?.videoWidth}x{videoRef.current?.videoHeight}</div>
          <div>{t('stats_speed')}: {state.playbackRate}x</div>
          <div>{t('stats_buffer')}: {Math.round(state.buffered)}%</div>
          <div>{t('stats_volume')}: {Math.round(state.volume * 100)}%</div>
          <div>{t('stats_state')}: {state.isPlaying ? t('stats_play') : t('stats_pause')}</div>
        </div>
      ) : null}

      {!state.isPlaying && !state.isBuffering && !state.playbackError && !state.showSettings && (
        <div className="absolute inset-0 z-20 flex items-center justify-center pointer-events-none" aria-hidden="true">
          <div className="w-20 h-20 bg-black/30 backdrop-blur-sm rounded-full flex items-center justify-center border border-white/10 shadow-[0_0_30px_rgba(0,0,0,0.3)]">
            <Play size={40} className="ml-2 text-white/90" fill="currentColor" />
          </div>
        </div>
      )}

      <div className={`absolute top-0 left-0 right-0 ${isCompact ? 'p-3' : 'p-6'} flex justify-between items-start z-50 transition-transform duration-300 ${state.showControls ? 'translate-y-0' : '-translate-y-20'}`}>
        <div className="flex flex-col min-w-0">
          <h1 className={`font-bold text-white tracking-tight truncate ${isCompact ? 'text-base' : 'text-xl md:text-2xl'}`}>{title}</h1>
          {nowPlayingLabel && <span className="text-xs text-indigo-300 font-medium tracking-wider uppercase mt-1 truncate" title={nowPlayingLabel}>{nowPlayingLabel}</span>}
        </div>
        <button
          onClick={(e) => {
            e.stopPropagation();
            handleClose();
          }}
          className={`group ${isCompact ? 'p-1.5' : 'p-2'} rounded-full bg-black/20 hover:bg-white/20 backdrop-blur-md border border-white/5 transition-all hover:scale-105`}
          aria-label={t('close_player')}
          title={t('close')}
        >
          <X size={isCompact ? 20 : 24} className="text-slate-200 group-hover:text-white" />
        </button>
      </div>

      <div
        className={`absolute bottom-0 left-0 right-0 ${isCompact ? 'px-3 pb-3 pt-7' : 'px-6 pb-6 pt-12'} z-50 transition-all duration-300 ${
          state.showControls ? 'translate-y-0 opacity-100' : 'translate-y-10 opacity-0 pointer-events-none'
        }`}
        onClick={(e) => e.stopPropagation()}
        style={isCompact ? { paddingBottom: 'calc(0.75rem + env(safe-area-inset-bottom))' } : undefined}
      >
        {state.isLocked ? (
          <div className="mb-3 flex justify-center">
            <div className="px-4 py-2 rounded-full bg-indigo-600/90 text-white text-xs font-bold border border-indigo-400/30 shadow-lg flex items-center gap-2">
              <Lock size={16} /> {t('controls_locked')}
            </div>
          </div>
        ) : null}

        <div className={state.isLocked ? 'pointer-events-none opacity-30 grayscale' : ''}>
        {episodesPanelOpen && Array.isArray(episodeSeasons) && episodeSeasons.length > 0 ? (
          <div className={`absolute ${isCompact ? 'left-3 right-3 bottom-24 w-auto max-h-[56vh]' : 'right-6 bottom-28 w-72 max-h-[52vh]'} bg-slate-950/90 border border-white/10 rounded-2xl backdrop-blur-xl shadow-2xl overflow-hidden transition-opacity ${
            state.showControls ? 'opacity-100' : 'opacity-0 pointer-events-none'
          }`}>
            <div className="px-3 py-2 border-b border-white/10 flex items-center justify-between">
              <div className="text-[11px] text-slate-400">{t('episodes')}</div>
              <div className="text-[11px] text-slate-400">{t('list')}</div>
            </div>
            <div className="max-h-[46vh] overflow-y-auto">
              {episodeSeasons.map((season) => {
                const open = openSeasonKey === season.key;
                return (
                  <div key={season.key} className="border-b border-white/5">
                    <button
                      onClick={() => {
                        setOpenSeasonKey((prev) => {
                          const isOpen = prev === season.key;
                          if (isOpen) {
                            userCollapsedAllSeasonsRef.current = true;
                            return null;
                          }
                          userCollapsedAllSeasonsRef.current = false;
                          return season.key;
                        });
                      }}
                      className="w-full px-3 py-2 flex items-center justify-between text-left text-xs font-semibold text-slate-200 hover:bg-white/5"
                    >
                      <span>{season.label}</span>
                      <ChevronRight className={`transition-transform ${open ? 'rotate-90' : ''}`} size={14} />
                    </button>
                    {open ? (
                      <div className="pb-2">
                        {season.episodes.map((f: any, idx: number) => {
                          let ep = (typeof f?.index === 'number' ? f.index : (typeof f?.file_index === 'number' ? f.file_index : idx + 1)) as number;
                          if (Number.isFinite(ep) && ep >= 1000) {
                            ep = ep % 1000;
                          }
                          const title =
                            f?.episodeTitle ||
                            f?.title ||
                            f?.name ||
                            f?.filename ||
                            (f?.path ? String(f.path).split(/[\\/]/).pop() : '') ||
                            `${t('episode')} ${ep}`;
                          const active = currentPath && f?.path && String(f.path) === String(currentPath);
                          return (
                            <button
                              key={`${season.key}-${idx}`}
                              onClick={() => onSelectEpisode?.(season.key, f)}
                              className={`w-full text-left px-3 py-2 flex items-start gap-2 border-t border-white/5 transition ${
                                active ? 'bg-indigo-600/20 text-white' : 'hover:bg-white/5 text-slate-300'
                              }`}
                            >
                              <div className={`text-xs font-bold w-6 ${active ? 'text-indigo-300' : 'text-slate-500'}`}>{ep}.</div>
                              <div className="text-xs font-semibold leading-tight line-clamp-2">{title}</div>
                              {active ? <Play size={14} className="ml-auto text-indigo-300" /> : null}
                            </button>
                          );
                        })}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        ) : null}

        <ProgressBar
          progress={state.progress}
          buffered={state.buffered}
          currentTime={state.currentTime}
          duration={state.duration}
          onSeek={handleSeek}
          compact={isCompact}
        />

        <ControlButtons
          isPlaying={state.isPlaying}
          onPlayToggle={togglePlay}
          onSkip={skip}
          onPrevItem={onPrevItem}
          onNextItem={onNextItem}
          hasPrevItem={hasPrevItem}
          hasNextItem={hasNextItem}
          volume={state.volume}
          isMuted={state.isMuted}
          onVolumeChange={handleVolumeChange}
          onMuteToggle={toggleMute}
          playbackRate={state.playbackRate}
          quality={state.quality}
          brightness={brightness}
          contrast={contrast}
          onPlaybackRateChange={handlePlaybackRateChange}
          onQualityChange={handleQualityChange}
          onBrightnessChange={setBrightness}
          onContrastChange={setContrast}
          showSettings={state.showSettings}
          onSettingsToggle={() => dispatch({ type: 'TOGGLE_SETTINGS' })}
          subtitlesAvailable={!!subtitlesUrl}
          subtitlesEnabled={state.subtitlesEnabled}
          onSubtitlesToggle={toggleSubtitles}
          episodesAvailable={Array.isArray(episodeSeasons) && episodeSeasons.length > 0}
          episodesOpen={episodesPanelOpen}
          onEpisodesToggle={() => {
            dispatch({ type: 'SET_SHOW_CONTROLS', payload: true });
            dispatch({ type: 'SET_IDLE', payload: false });
            setEpisodesPanelOpen((v) => {
              const next = !v;
              if (next) userCollapsedAllSeasonsRef.current = false;
              return next;
            });
          }}
          isPip={state.isPip}
          onPipToggle={() => void togglePip()}
          showStats={state.showStats}
          onStatsToggle={() => dispatch({ type: 'TOGGLE_STATS' })}
          isFullscreen={state.isFullscreen}
          onFullscreenToggle={toggleFullscreen}
          isLocked={state.isLocked}
          onLockToggle={() => dispatch({ type: 'TOGGLE_LOCK' })}
          compact={isCompact}
        />
        </div>
      </div>

      <button
        onClick={(e) => {
          e.stopPropagation();
          dispatch({ type: 'TOGGLE_LOCK' });
        }}
        className={`absolute ${isCompact ? 'right-3 bottom-24 p-3 rounded-xl' : 'left-6 top-1/2 -translate-y-1/2 p-4 rounded-2xl'} bg-black/40 backdrop-blur-xl border border-white/10 transition-all duration-300 z-[55] ${
          state.showControls ? (isCompact ? 'translate-y-0 opacity-100' : 'translate-x-0 opacity-100') : (isCompact ? 'translate-y-8 opacity-0' : '-translate-x-16 opacity-0')
        }`}
        aria-label={state.isLocked ? t('unlock_controls') : t('lock_controls')}
        title={state.isLocked ? t('unlock_controls_hint') : t('lock_controls_hint')}
      >
        {state.isLocked ? <Lock className="text-indigo-400" size={isCompact ? 20 : 24} /> : <Unlock className="text-white/60" size={isCompact ? 20 : 24} />}
      </button>
    </div>
  );
};
