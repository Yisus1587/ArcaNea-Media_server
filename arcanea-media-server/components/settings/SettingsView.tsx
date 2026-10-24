import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  HardDrive, FolderPlus, Trash2, Database, ScanLine, Loader2,
  RefreshCw, CheckCircle, AlertCircle, Plus, Save, Users, Pencil, X, FolderPlus as FolderPlusIcon,
  Languages,
} from 'lucide-react';
import { Button } from '../ui/Button';
import { Drive, ScanStatus, MetadataConfig, UserProfile, TelemetryEvent, TelemetryTask, ConflictGroup, AdminEntityRow } from '../../types';
import { useI18n } from '../../i18n/i18n';
import { MediaService } from '../../services/api';
import { FolderPickerModal } from './FolderPickerModal';
import { HelpModal } from './HelpModal';
import { NoMatchModal } from './NoMatchModal';
import { SystemStatusDashboard } from './sections/SystemStatusDashboard';
import { MetadataProviderConfig } from './sections/MetadataProviderConfig';
import { ActivityFeed } from './sections/ActivityFeed';
import { EnrichmentTuning } from './sections/EnrichmentTuning';

interface SettingsViewProps {
  drives: Drive[];
  libraryPaths: string[];
  looseMovieRoots: string[];
  rootTypes: Record<string, string>;
  scanStatus: ScanStatus;
  metadataConfig: MetadataConfig;
  profiles: UserProfile[];
  onAddPath: (path: string, type?: string) => void;
  onRemovePath: (path: string) => void;
  onUpdateConfig: (config: MetadataConfig) => void;
  onToggleLooseMovieRoot: (path: string) => void;
  onSetRootType: (path: string, type: string) => void;
  onStartScan: () => void;
  onRefresh?: () => void;
  onAddProfile: (profile: Omit<UserProfile, 'id'>) => void;
  onUpdateProfile: (profile: UserProfile) => void;
  onDeleteProfile: (id: string) => void;
  enrichment?: any;
  migrationRunning?: boolean;
  migrationError?: string | null;
  onStartEnrich?: () => void;
}

const AVATAR_COLORS = [
  'bg-indigo-600', 'bg-purple-600', 'bg-pink-600', 'bg-red-600',
  'bg-orange-600', 'bg-yellow-500', 'bg-green-600', 'bg-blue-600', 'bg-slate-600'
];

export const SettingsView: React.FC<SettingsViewProps> = ({
  drives,
  libraryPaths,
  rootTypes,
  scanStatus,
  metadataConfig,
  profiles,
  onAddPath,
  onRemovePath,
  onUpdateConfig,
  onSetRootType,
  onStartScan,
  onRefresh,
  onAddProfile,
  onUpdateProfile,
  onDeleteProfile,
  enrichment,
  migrationRunning,
  migrationError,
  onStartEnrich,
}) => {
  const { lang, setLang, t } = useI18n();
  const [newPathInput, setNewPathInput] = useState('');
  const [newPathType, setNewPathType] = useState('auto');
  const [tmdbConfigured, setTmdbConfigured] = useState<boolean | null>(null);
  const [tmdbTestResult, setTmdbTestResult] = useState<string | null>(null);
  const [showTmdbCredentials, setShowTmdbCredentials] = useState(false);
  const [tmdbApiKeyInput, setTmdbApiKeyInput] = useState('');
  const [tmdbAccessTokenInput, setTmdbAccessTokenInput] = useState('');
  const [tmdbUseV4, setTmdbUseV4] = useState(false);
  const [tmdbCredMsg, setTmdbCredMsg] = useState<string | null>(null);
  const [tmdbBusy, setTmdbBusy] = useState<'idle' | 'validating' | 'saving'>('idle');
  const [showTmdbSecrets, setShowTmdbSecrets] = useState(false);
  const [activeTab, setActiveTab] = useState<'general' | 'control'>('general');

  // Profile Management State
  const [editingProfile, setEditingProfile] = useState<Partial<UserProfile> | null>(null);
  const [isCreatingProfile, setIsCreatingProfile] = useState(false);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [livePaused, setLivePaused] = useState(false);
  const [showFolderPicker, setShowFolderPicker] = useState(false);
  const [showHelpModal, setShowHelpModal] = useState(false);
  const [showNoMatchModal, setShowNoMatchModal] = useState(false);
  const [retryProviderMode, setRetryProviderMode] = useState<'auto' | 'jikan' | 'kitsu' | 'tmdb'>('auto');
  const [showSystemLogs, setShowSystemLogs] = useState(false);
  const [logsTail, setLogsTail] = useState<{ path: string; lines: string[]; error?: string } | null>(null);
  const [health, setHealth] = useState<any | null>(null);
  const [enrichHistory, setEnrichHistory] = useState<any[]>([]);
  const [jikanFetchEpisodes, setJikanFetchEpisodes] = useState(false);
  const [enrichRatePerMin, setEnrichRatePerMin] = useState('60');
  const [enrichBatchSize, setEnrichBatchSize] = useState('20');
  const [enrichTuningBusy, setEnrichTuningBusy] = useState(false);
  const [enrichTuningMsg, setEnrichTuningMsg] = useState<string | null>(null);
  const [enrichPreset, setEnrichPreset] = useState<'safe' | 'balanced' | 'fast'>('balanced');
  const [showEnrichAdvanced, setShowEnrichAdvanced] = useState(false);
  const [scanItems, setScanItems] = useState<Array<{ id: number | string; title?: string | null; path?: string | null; media_root?: string | null; media_type?: string | null }>>([]);
  const [scanItemsTotal, setScanItemsTotal] = useState(0);
  const [scanItemsBusy, setScanItemsBusy] = useState(false);
  const [scanItemsError, setScanItemsError] = useState<string | null>(null);
  const [controlQuery, setControlQuery] = useState('');
  const [controlResults, setControlResults] = useState<AdminEntityRow[]>([]);
  const [controlTotal, setControlTotal] = useState(0);
  const [controlLoading, setControlLoading] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  const [conflicts, setConflicts] = useState<{ path_hash: ConflictGroup[]; tmdb_id: ConflictGroup[] } | null>(null);
  const [conflictsLoading, setConflictsLoading] = useState(false);
  const [mergeDrafts, setMergeDrafts] = useState<Record<string, { keepId?: number; dropId?: number }>>({});
  const [resetConfirm, setResetConfirm] = useState('');
  const [resetBusy, setResetBusy] = useState(false);
  const [queueBusy, setQueueBusy] = useState(false);
  const [telemetryConnected, setTelemetryConnected] = useState(false);
  const [telemetryTasks, setTelemetryTasks] = useState<Record<string, TelemetryTask>>({});
  const [logEvents, setLogEvents] = useState<TelemetryEvent[]>([]);
  const [apiEvents, setApiEvents] = useState<TelemetryEvent[]>([]);
  const [logFilter, setLogFilter] = useState<'all' | 'error'>('all');
  const [logTaskId, setLogTaskId] = useState<string | null>(null);
  const prevPending = useRef<number | null>(null);
  const prevCurrentId = useRef<number | null>(null);
  const [liveEnrichment, setLiveEnrichment] = useState<any | null>(enrichment || null);
  const pollRef = useRef<number | null>(null);
  const enrich = liveEnrichment || enrichment || null;

  const TELEMETRY_PHASES = ['Discovery', 'Hashing', 'Jikan_Search', 'TMDB_Enrichment', 'DB_Commit'];
  const localizeStatus = { disabled: true };
  const animeNormalizeStatus = { disabled: true };
  const loc = localizeStatus;
  const an = animeNormalizeStatus;
  const locTotal = 0;
  const locProcessed = 0;
  const anTotal = 0;
  const anProcessed = 0;
  const locPct = 0;
  const anPct = 0;
  const locRunning = false;
  const anRunning = false;
  const lenguajeBusy: string = 'disabled';
  const refreshLenguajeStatus = async () => undefined;
  const handleStartAnimeNormalize = async () => undefined;
  const handleStopAnimeNormalize = async () => undefined;
  const handleRunAnimeTranslations = async () => undefined;

  const applyTelemetryEvent = (raw: TelemetryEvent) => {
    if (!raw || !raw.task_id) return;
    const ev = raw as TelemetryEvent;
    setTelemetryTasks(prev => {
      const next = { ...(prev || {}) } as Record<string, TelemetryTask>;
      const existing = next[ev.task_id] || { task_id: ev.task_id };
      const t: TelemetryTask = { ...existing };
      if (ev.task_kind) t.task_kind = ev.task_kind;
      if (ev.title) t.title = ev.title;
      if (ev.media_item_id != null) t.media_item_id = ev.media_item_id;
      if (ev.type === 'task.start' || ev.type === 'task.queued') {
        t.status = ev.status || 'running';
      }
      if (ev.type === 'task.end') {
        t.status = ev.status || 'done';
        t.current_phase = null;
      }
      if (ev.type === 'task.phase' && ev.phase) {
        const phases = { ...(t.phases || {}) };
        const ph = { ...(phases[ev.phase] || {}) };
        if (ev.status) {
          if (ev.status === 'enter') ph.status = 'running';
          else if (ev.status === 'exit') ph.status = 'done';
          else ph.status = ev.status;
        }
        if (typeof ev.duration_ms === 'number') ph.duration_ms = ev.duration_ms;
        if (ev.ts) {
          if (ev.status === 'enter') ph.started_at = ev.ts;
          if (ev.status === 'exit') ph.finished_at = ev.ts;
        }
        phases[ev.phase] = ph;
        t.phases = phases;
        if (ev.status === 'enter') t.current_phase = ev.phase;
      }
      t.last_updated = ev.ts || t.last_updated || Math.floor(Date.now() / 1000);
      next[ev.task_id] = t;
      return next;
    });
  };

  const handleAddPath = (e: React.FormEvent) => {
    e.preventDefault();
    if (newPathInput.trim()) {
      onAddPath(newPathInput, newPathType);
      setNewPathInput('');
    }
  };

  useEffect(() => {
    let active = true;
    const tick = async () => {
      try {
        const s = await MediaService.getCredentialsStatus();
        if (!active) return;
        setTmdbConfigured(!!s.tmdb_configured);
      } catch {
        if (!active) return;
        setTmdbConfigured(false);
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 5000);
    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const cfg = await MediaService.getAppConfig();
        if (!active) return;
        let rateFromCfg: number | null = null;
        let batchFromCfg: number | null = null;
        if (cfg && typeof (cfg as any).jikan_fetch_episodes !== 'undefined') {
          setJikanFetchEpisodes(!!(cfg as any).jikan_fetch_episodes);
        }
        if (cfg && typeof (cfg as any).enrichment_rate_per_min !== 'undefined') {
          const v = Number((cfg as any).enrichment_rate_per_min);
          if (Number.isFinite(v) && v > 0) {
            rateFromCfg = v;
            setEnrichRatePerMin(String(v));
          }
        }
        if (cfg && typeof (cfg as any).enrichment_batch_size !== 'undefined') {
          const v = Number((cfg as any).enrichment_batch_size);
          if (Number.isFinite(v) && v > 0) {
            batchFromCfg = v;
            setEnrichBatchSize(String(v));
          }
        }
        if (rateFromCfg !== null || batchFromCfg !== null) {
          const rate = (rateFromCfg ?? Number(enrichRatePerMin)) || 60;
          const batch = (batchFromCfg ?? Number(enrichBatchSize)) || 20;
          if (rate >= 120 || batch >= 40) setEnrichPreset('fast');
          else if (rate <= 60 && batch <= 20) setEnrichPreset('safe');
          else setEnrichPreset('balanced');
        }
      } catch {}
    })();
    return () => { active = false; };
  }, []);

  const refreshSystemInfo = async () => {
    try {
      const h = await MediaService.getHealth();
      setHealth(h || null);
    } catch {
      setHealth(null);
    }
  };

  const refreshLogs = async () => {
    try {
      const t = await MediaService.getLogsTail(250);
      setLogsTail(t || null);
    } catch {
      setLogsTail({ path: '', lines: [], error: 'failed' });
    }
  };

  useEffect(() => {
    void refreshSystemInfo();
    if (activeTab === 'control') void refreshLogs();
    const id = window.setInterval(() => {
      void refreshSystemInfo();
      if (activeTab === 'control') void refreshLogs();
    }, activeTab === 'control' ? 10000 : 15000);
    return () => window.clearInterval(id);
  }, [activeTab]);


  useEffect(() => {
    if (!enrichment) return;
    setLiveEnrichment(prev => ({ ...(prev || {}), ...(enrichment || {}) }));
    if (livePaused) return;
    try {
      const now = Date.now();
      const curId = enrichment.current_id ? Number(enrichment.current_id) : null;
      if (curId && curId !== prevCurrentId.current) {
        prevCurrentId.current = curId;
        setEnrichHistory(h => [{ type: 'processing', id: curId, title: enrichment.current_title || null, ts: now }, ...h].slice(0, 200));
      }
      if (typeof enrichment.pending === 'number') {
        const p = Number(enrichment.pending);
        if (prevPending.current != null && p < prevPending.current) {
          const diff = prevPending.current - p;
          setEnrichHistory(h => [{ type: 'ingested', count: diff, ts: now }, ...h].slice(0, 200));
        }
        prevPending.current = p;
      }
    } catch {}
  }, [enrichment, livePaused]);

  useEffect(() => {
    let mounted = true;
    const startPolling = async () => {
      try {
        const fetchOnce = async () => {
          try {
            const data = await MediaService.getEnrichStatus();
            if (!mounted) return;
            setLiveEnrichment(prev => ({ ...(prev || {}), ...(data || {}) }));
            const now = Date.now();
            const curId = data.current_id ? Number(data.current_id) : null;
            if (curId && curId !== prevCurrentId.current) {
              prevCurrentId.current = curId;
              setEnrichHistory(h => [{ type: 'processing', id: curId, title: data.current_title || null, ts: now }, ...h].slice(0, 200));
            }
            if (typeof data.pending === 'number') {
              const p = Number(data.pending);
              if (prevPending.current != null && p < prevPending.current) {
                const diff = prevPending.current - p;
                setEnrichHistory(h => [{ type: 'ingested', count: diff, ts: now }, ...h].slice(0, 200));
              }
              prevPending.current = p;
            }
          } catch {}
        };
        await fetchOnce();
        if (pollRef.current) window.clearInterval(pollRef.current);
        pollRef.current = window.setInterval(() => {
          if (!livePaused) void fetchOnce();
        }, 2000) as any;
      } catch {}
    };
    startPolling();
    return () => {
      mounted = false;
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [livePaused]);

  useEffect(() => {
    let stopped = false;
    let es: EventSource | null = null;
    let attempt = 0;
    const connect = async () => {
      try {
        const urls = MediaService.getTelemetrySseUrls();
        if (!urls.length) { setTelemetryConnected(false); return; }
        es = new EventSource(urls[attempt % urls.length]);
        es.onopen = () => setTelemetryConnected(true);
        es.onerror = () => {
          setTelemetryConnected(false);
          try { es?.close(); } catch {}
          attempt++;
          if (!stopped) window.setTimeout(connect, 2000);
        };
        const handle = (event: MessageEvent) => {
          if (!event?.data) return;
          let data: any = null;
          try { data = JSON.parse(event.data); } catch { return; }
          const type = data?.type || event.type || 'message';
          if (type === 'snapshot') {
            const tasks = Array.isArray(data?.tasks) ? data.tasks : [];
            const map: Record<string, TelemetryTask> = {};
            for (const t of tasks) if (t && t.task_id) map[t.task_id] = t as TelemetryTask;
            setTelemetryTasks(map);
            if (Array.isArray(data?.logs)) setLogEvents(data.logs.slice().reverse());
            return;
          }
          const ev: TelemetryEvent = { ...(data || {}), type };
          if (type === 'log') setLogEvents(prev => [ev, ...(prev || [])].slice(0, 300));
          if (type === 'provider_http' || type === 'api.timing') setApiEvents(prev => [ev, ...(prev || [])].slice(0, 120));
          if (ev.task_id) applyTelemetryEvent(ev);
        };
        es.addEventListener('snapshot', handle as any);
        es.addEventListener('task.phase', handle as any);
        es.addEventListener('task.start', handle as any);
        es.addEventListener('task.end', handle as any);
        es.addEventListener('task.queued', handle as any);
        es.addEventListener('log', handle as any);
        es.addEventListener('provider_http', handle as any);
        es.addEventListener('api.timing', handle as any);
        es.onmessage = handle as any;
      } catch { setTelemetryConnected(false); }
    };
    connect();
    return () => { stopped = true; try { es?.close(); } catch {} setTelemetryConnected(false); };
  }, []);

  useEffect(() => {
    if (activeTab !== 'control') return;
    void runControlSearch();
    void loadConflicts();
  }, [activeTab]);

  const handleTestTmdb = async () => {
    setTmdbTestResult('testing');
    try {
      const r = await MediaService.getCredentialsCheck();
      if (r.ok) setTmdbTestResult('ok');
      else setTmdbTestResult(r.detail || 'failed');
    } catch (e) { setTmdbTestResult(String(e)); }
    try {
      const s = await MediaService.getCredentialsStatus();
      setTmdbConfigured(!!s.tmdb_configured);
    } catch {}
  };

  const buildTmdbPayload = () => {
    const payload: any = { tmdb_use_v4: !!tmdbUseV4 };
    const key = (tmdbApiKeyInput || '').trim();
    const token = (tmdbAccessTokenInput || '').trim();
    if (key) payload.tmdb_api_key = key;
    if (token) payload.tmdb_access_token = token.toLowerCase().startsWith('bearer ') ? token.slice(7).trim() : token;
    return payload;
  };

  const handleRetryNoMatch = async () => {
    try {
      const api = MediaService;
      const r = await api.resetNoMatchWithOptions({ animeProvider: retryProviderMode, start: true });
      try {
        const resetCount = Number((r as any)?.data?.reset ?? 0);
        setEnrichHistory(h => [{ type: 'reset_no_match', count: resetCount, ts: Date.now() }, ...h].slice(0, 200));
      } catch (e) {
        // ignore
      }
      try {
        const data = await api.getEnrichStatus();
        setLiveEnrichment(prev => ({ ...(prev || {}), ...(data || {}) }));
      } catch (e) {
        // ignore
      }
    } catch (e) {
      // ignore
    }
  };

  const handleRetryError = async () => {
    try {
      const api = MediaService;
      const r = await api.resetError({ animeProvider: retryProviderMode, start: true });
      try {
        const resetCount = Number((r as any)?.data?.reset ?? 0);
        setEnrichHistory(h => [{ type: 'reset_error', count: resetCount, ts: Date.now() }, ...h].slice(0, 200));
      } catch {}
      try {
        const data = await api.getEnrichStatus();
        setLiveEnrichment(prev => ({ ...(prev || {}), ...(data || {}) }));
      } catch {}
    } catch {}
  };

  const handleSaveEnrichTuning = async (override?: { rate?: number; batch?: number; jikan?: boolean }) => {
    setEnrichTuningMsg(null);
    setEnrichTuningBusy(true);
    try {
      const cfg = await MediaService.getAppConfig();
      const rate = Math.max(10, Math.min(300, Math.floor(Number(override?.rate ?? enrichRatePerMin) || 60)));
      const batch = Math.max(5, Math.min(200, Math.floor(Number(override?.batch ?? enrichBatchSize) || 20)));
      const jikan = typeof override?.jikan === 'boolean' ? override.jikan : !!jikanFetchEpisodes;
      await MediaService.saveAppConfig({ ...(cfg || {}), jikan_fetch_episodes: jikan, enrichment_rate_per_min: rate, enrichment_batch_size: batch });
      setEnrichRatePerMin(String(rate));
      setEnrichBatchSize(String(batch));
      setJikanFetchEpisodes(jikan);
      setEnrichTuningMsg(t('saved'));
    } catch { setEnrichTuningMsg(t('process_failed')); }
    finally { setEnrichTuningBusy(false); }
  };

  const applyEnrichPreset = async (pId: 'safe' | 'balanced' | 'fast') => {
    const presets: Record<string, { rate: number; batch: number; jikan: boolean }> = {
      safe: { rate: 60, batch: 20, jikan: false },
      balanced: { rate: 90, batch: 30, jikan: false },
      fast: { rate: 120, batch: 40, jikan: false },
    };
    const p = presets[pId];
    setEnrichPreset(pId);
    setEnrichRatePerMin(String(p.rate));
    setEnrichBatchSize(String(p.batch));
    setJikanFetchEpisodes(p.jikan);
    await handleSaveEnrichTuning(p);
  };

  const runControlSearch = async (q?: string) => {
    setControlLoading(true); setControlError(null);
    try {
      const res = await MediaService.searchEntities(q !== undefined ? q : controlQuery, 50, 0);
      setControlResults(Array.isArray(res?.items) ? res.items : []);
      setControlTotal(Number(res?.total || 0));
    } catch (e) { setControlError(String(e)); }
    finally { setControlLoading(false); }
  };

  const loadConflicts = async () => {
    setConflictsLoading(true);
    try {
      const res = await MediaService.getConflicts('all');
      setConflicts(res?.conflicts || null);
    } catch { setConflicts(null); }
    finally { setConflictsLoading(false); }
  };

  const handleDeepPurge = async (id: number) => {
    if (!window.confirm(t('control_confirm_purge'))) return;
    try {
      await MediaService.deepPurgeEntity(id);
      void runControlSearch(); void loadConflicts();
    } catch (e) { setControlError(String(e)); }
  };

  const handleRematch = async (item: AdminEntityRow) => {
    const tmdbId = window.prompt(t('control_tmdb_prompt'), String(item.tmdb_id || ''));
    if (!tmdbId || !tmdbId.trim()) return;
    const mediaType = window.prompt(t('control_media_type_prompt'), String(item.media_type || 'tv')) || undefined;
    const seasonRaw = window.prompt(t('control_season_prompt'), '');
    let seasonNumber: number | undefined = undefined;
    if (seasonRaw && String(seasonRaw).trim()) {
      const n = Number(seasonRaw); if (Number.isFinite(n)) seasonNumber = n;
    }
    try {
      await MediaService.rematchEntity(Number(item.id), { tmdb_id: tmdbId.trim(), media_type: mediaType?.trim(), season_number: seasonNumber });
      void runControlSearch(); void loadConflicts();
    } catch (e) { setControlError(String(e)); }
  };

  const handleMerge = async (groupKey: string) => {
    const draft = mergeDrafts[groupKey];
    if (!draft?.keepId || !draft?.dropId || draft.keepId === draft.dropId) return;
    try {
      await MediaService.mergeConflicts(draft.keepId, draft.dropId);
      void loadConflicts(); void runControlSearch();
    } catch (e) { setControlError(String(e)); }
  };

  const handleValidateTmdbCredentials = async () => {
    const payload = buildTmdbPayload();
    if (!payload.tmdb_api_key && !payload.tmdb_access_token) { setTmdbCredMsg(t('tmdb_enter_api_or_token')); return; }
    setTmdbBusy('validating'); setTmdbCredMsg(null);
    try {
      const r = await MediaService.checkCredentials(payload);
      if (r.ok) { setTmdbCredMsg(t('tmdb_creds_valid')); setTmdbTestResult('ok'); }
      else { setTmdbCredMsg(`${t('tmdb_validation_failed')}: ${r.detail || 'failed'}`); setTmdbTestResult(r.detail || 'failed'); }
    } catch (e) { setTmdbCredMsg(`${t('tmdb_validation_failed')}: ${String(e)}`); setTmdbTestResult(String(e)); }
    finally {
      setTmdbBusy('idle');
      try { const s = await MediaService.getCredentialsStatus(); setTmdbConfigured(!!s.tmdb_configured); } catch {}
    }
  };

  const handleSaveTmdbCredentials = async () => {
    const payload = buildTmdbPayload();
    if (!payload.tmdb_api_key && !payload.tmdb_access_token && !payload.tmdb_use_v4) { setTmdbCredMsg(t('nothing_to_save')); return; }
    setTmdbBusy('saving'); setTmdbCredMsg(null);
    try {
      const resp = await MediaService.saveCredentials(payload);
      if (resp?.saved) {
        if (resp.tmdb_ok) {
          setTmdbCredMsg(`${t('saved_ok')} ${t('tmdb_ok')}. ${resp.enrichment_started ? t('enrich_started') : t('enrich_already_running')}`);
          setTmdbTestResult('ok'); setTmdbApiKeyInput(''); setTmdbAccessTokenInput(''); setShowTmdbSecrets(false); setShowTmdbCredentials(false);
        } else { setTmdbCredMsg(`${t('tmdb_saved_but_check_failed')}: ${resp.detail || 'failed_check'}`); setTmdbTestResult(resp.detail || 'failed_check'); }
      } else { setTmdbCredMsg(t('tmdb_save_failed')); }
    } catch (e) { setTmdbCredMsg(`${t('tmdb_save_failed')}: ${String(e)}`); }
    finally {
      setTmdbBusy('idle');
      try { const s = await MediaService.getCredentialsStatus(); setTmdbConfigured(!!s.tmdb_configured); } catch {}
    }
  };

  const handleProfileSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingProfile?.name) return;
    const pin = String((editingProfile as any).pin || '').replace(/\D/g, '').slice(0, 6) || undefined;
    const isManager = !!(editingProfile as any).isManager && !(editingProfile as any).isKid;
    if (isManager && (!pin || pin.length < 4)) { window.alert(t('manager_pin_required_alert')); return; }
    if (!isManager && pin && pin.length < 4) { window.alert(t('pin_length_alert')); return; }
    if (isCreatingProfile) onAddProfile({ name: editingProfile.name, avatarColor: editingProfile.avatarColor || AVATAR_COLORS[0], avatarImage: (editingProfile as any).avatarImage, isKid: editingProfile.isKid || false, isManager, pin, language: editingProfile.language || 'en-US' });
    else if (editingProfile.id) onUpdateProfile({ ...(editingProfile as any), isManager, pin } as UserProfile);
    setEditingProfile(null); setIsCreatingProfile(false);
  };

  const startCreateProfile = () => {
    setEditingProfile({ name: '', avatarColor: AVATAR_COLORS[Math.floor(Math.random() * AVATAR_COLORS.length)], isKid: false, isManager: false, language: 'en-US' });
    setIsCreatingProfile(true);
  };

  const startEditProfile = (profile: UserProfile) => { setEditingProfile({ ...profile }); setIsCreatingProfile(false); };

  const telemetryTaskList = (Object.values(telemetryTasks) as TelemetryTask[]).sort((a, b) => Number(b.last_updated || 0) - Number(a.last_updated || 0)).slice(0, 10);
  const telemetryTitleCounts = telemetryTaskList.reduce((acc, t) => { const key = (t.title || '').trim(); if (key) acc[key] = (acc[key] || 0) + 1; return acc; }, {} as Record<string, number>);
  const filteredLogs = (logEvents || []).filter(ev => {
    if (logFilter === 'error') { const lvl = String(ev.level || '').toUpperCase(); if (!lvl.includes('ERROR') && !lvl.includes('WARN')) return false; }
    if (logTaskId && ev.task_id !== logTaskId) return false;
    return true;
  }).slice(0, 200);
  const latestApiEvent = useMemo(
    () => (apiEvents || []).find((ev) => !!String(ev.provider || ev.service || '').trim()) || null,
    [apiEvents],
  );
  const configuredAnimeProvider = String(metadataConfig?.animeProvider || health?.provider_config?.anime || '').trim().toUpperCase() || null;
  const currentApiProvider = useMemo(() => {
    const step = String((enrich as any)?.current_step || '').trim().toLowerCase();
    if (step.startsWith('search:')) return step.slice(7).toUpperCase();
    const raw = String(latestApiEvent?.provider || latestApiEvent?.service || '').trim();
    return raw ? raw.toUpperCase() : null;
  }, [enrich, latestApiEvent]);
  const kitsuHealth = health?.providers?.kitsu || null;
  const jikanHealth = health?.providers?.jikan || null;
  const recentApiActivity = useMemo(
    () => (apiEvents || []).filter((ev) => ev.type === 'provider_http' || ev.type === 'api.timing').slice(0, 8),
    [apiEvents],
  );

  return (
    <div className="max-w-7xl mx-auto py-10 px-4 sm:px-8 space-y-10 pb-32 animate-fade-in">
      <div className="relative overflow-hidden rounded-3xl border border-white/5 bg-gradient-to-br from-[#0b1020] via-[#101826] to-[#0b0f1a] p-6 sm:p-8 shadow-[0_30px_80px_-50px_rgba(15,23,42,0.9)]">
        <div className="absolute -top-24 right-0 h-48 w-48 rounded-full bg-gradient-to-br from-sky-500/30 via-cyan-500/10 to-transparent blur-3xl" />
        <div className="absolute -bottom-32 left-0 h-56 w-56 rounded-full bg-gradient-to-br from-amber-500/20 via-orange-500/10 to-transparent blur-3xl" />
        <div className="relative flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-xs uppercase tracking-[0.4em] text-slate-500">{t('settings_title')}</div>
            <h1 className="mt-2 text-3xl sm:text-4xl font-semibold text-white">
              {t('settings_title')}
            </h1>
            <p className="mt-2 text-sm text-slate-400">
              {t('system_status')}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2 px-3 py-2 rounded-2xl bg-white/5 border border-white/10">
              <label className="text-[11px] font-semibold text-slate-400 uppercase tracking-[0.2em]">{t('app_language')}</label>
              <select value={lang} onChange={(e) => { const v = String(e.target.value || '').toLowerCase(); if (v === 'es' || v === 'en') setLang(v, 'manual'); }} className="bg-transparent text-sm text-slate-200 focus:outline-none cursor-pointer [&>option]:bg-[#0f172a]">
                <option value="es">{t('spanish')}</option><option value="en">English</option>
              </select>
            </div>
            <Button size="sm" variant="ghost" onClick={() => setShowHelpModal(true)}>{t('help')}</Button>
            <Button size="sm" onClick={() => onRefresh && onRefresh()} icon={<RefreshCw size={14} />}>{t('refresh')}</Button>
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={() => setActiveTab('general')}
          className={`px-5 py-2.5 rounded-full text-xs font-bold uppercase tracking-[0.25em] border transition-all ${activeTab === 'general' ? 'bg-gradient-to-r from-sky-500 to-cyan-500 text-slate-950 border-transparent shadow-lg shadow-cyan-500/20' : 'bg-white/5 text-slate-300 border-white/10 hover:border-white/30 hover:text-white'}`}
        >
          {t('general')}
        </button>
        <button
          onClick={() => setActiveTab('control')}
          className={`px-5 py-2.5 rounded-full text-xs font-bold uppercase tracking-[0.25em] border transition-all ${activeTab === 'control' ? 'bg-gradient-to-r from-amber-400 to-orange-500 text-slate-950 border-transparent shadow-lg shadow-orange-500/20' : 'bg-white/5 text-slate-300 border-white/10 hover:border-white/30 hover:text-white'}`}
        >
          {t('control_data')}
        </button>
      </div>

      <div className={activeTab === 'general' ? 'block' : 'hidden'}>
        <div className="space-y-8">
          {/* Profiles */}
          <div className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0f172a]/80 via-[#101827]/80 to-[#0a0f1b]/90 p-6 shadow-[0_25px_60px_-40px_rgba(15,23,42,0.9)]">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-semibold flex items-center text-white"><Users className="mr-3 text-cyan-400" /> {t('profiles')}</h2>
              {!editingProfile && <Button size="sm" onClick={startCreateProfile} icon={<Plus size={16} />}>{t('add_profile')}</Button>}
            </div>
            {editingProfile ? (
              <form onSubmit={handleProfileSubmit} className="bg-black/30 p-6 rounded-2xl border border-white/10">
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                  <div className="lg:col-span-1 space-y-4">
                    <div className="flex items-center gap-4">
                      <div className={`w-20 h-20 rounded-2xl flex items-center justify-center overflow-hidden shadow-lg ${editingProfile.avatarColor || AVATAR_COLORS[0]}`}>
                        {editingProfile.avatarImage ? (
                          <img src={editingProfile.avatarImage} alt={editingProfile.name || 'avatar'} className="w-full h-full object-cover" />
                        ) : (
                          <span className="text-3xl font-bold text-white">{editingProfile.name?.[0] || '?'}</span>
                        )}
                      </div>
                      <div className="flex-1">
                        <label className="block text-xs font-semibold text-slate-400 uppercase tracking-[0.2em]">{t('photo_optional')}</label>
                        <input
                          type="file"
                          accept="image/*"
                          disabled={avatarBusy}
                          onChange={async (e) => {
                            const f = e.target.files && e.target.files[0];
                            if (!f) return;
                            setAvatarBusy(true);
                            try {
                              const reader = new FileReader();
                              const dataUrl: string = await new Promise((resolve, reject) => {
                                reader.onerror = () => reject(new Error('read_failed'));
                                reader.onload = () => resolve(String(reader.result || ''));
                                reader.readAsDataURL(f);
                              });
                              setEditingProfile({ ...(editingProfile || {}), avatarImage: dataUrl });
                            } catch {
                              // ignore
                            } finally {
                              setAvatarBusy(false);
                              try { e.target.value = ''; } catch {}
                            }
                          }}
                          className="mt-2 block w-full text-xs text-slate-300 file:mr-3 file:py-2 file:px-3 file:rounded-md file:border-0 file:bg-slate-800 file:text-slate-200 hover:file:bg-slate-700"
                        />
                        {!!editingProfile.avatarImage && (
                          <button
                            type="button"
                            onClick={() => setEditingProfile({ ...(editingProfile || {}), avatarImage: undefined })}
                            className="mt-2 px-3 py-1.5 text-xs text-slate-300 hover:text-white rounded-md border border-white/10 hover:bg-white/5 transition-colors"
                          >
                            {t('remove')}
                          </button>
                        )}
                      </div>
                    </div>
                    <div>
                      <div className="text-[11px] font-semibold text-slate-400 uppercase tracking-[0.2em]">{t('avatar_color')}</div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        {AVATAR_COLORS.map((c) => (
                          <button
                            key={c}
                            type="button"
                            onClick={() => setEditingProfile({ ...(editingProfile || {}), avatarColor: c })}
                            className={`h-7 w-7 rounded-lg border ${editingProfile.avatarColor === c ? 'border-cyan-400 ring-2 ring-cyan-400/30' : 'border-white/10'} ${c}`}
                            aria-label={`avatar-${c}`}
                          />
                        ))}
                      </div>
                    </div>
                  </div>
                  <div className="lg:col-span-2 space-y-4">
                    <input
                      type="text"
                      value={editingProfile.name}
                      onChange={e => setEditingProfile({ ...editingProfile, name: e.target.value })}
                      className="w-full bg-black/40 border border-white/10 rounded-xl px-4 py-2.5 text-sm text-white focus:ring-2 focus:ring-cyan-500 outline-none"
                      placeholder={t('profile_name')}
                      required
                    />
                    <select
                      value={editingProfile.language}
                      onChange={e => setEditingProfile({ ...editingProfile, language: e.target.value })}
                      className="w-full bg-black/40 border border-white/10 rounded-xl px-4 py-2.5 text-sm text-white focus:ring-2 focus:ring-cyan-500 outline-none"
                    >
                      <option value="en-US">English (US)</option>
                      <option value="es-ES">Espanol (MX)</option>
                    </select>
                    <div className="flex items-start gap-3 rounded-xl border border-white/10 bg-black/40 px-4 py-3">
                      <input
                        type="checkbox"
                        checked={editingProfile.isKid}
                        onChange={e => setEditingProfile({ ...editingProfile, isKid: e.target.checked, ...(e.target.checked ? { isManager: false } : {}) })}
                        className="mt-1 rounded bg-slate-800 border-white/10"
                      />
                      <div>
                        <div className="text-sm text-slate-200">{t('kid_profile_restricted')}</div>
                      </div>
                    </div>
                    <div className="flex items-start gap-3 rounded-xl border border-white/10 bg-black/40 px-4 py-3">
                      <input
                        type="checkbox"
                        checked={!!(editingProfile as any).isManager}
                        onChange={e => setEditingProfile({ ...(editingProfile || {}), isManager: e.target.checked, ...(e.target.checked ? { isKid: false } : {}) })}
                        className="mt-1 rounded bg-slate-800 border-white/10"
                      />
                      <div>
                        <div className="text-sm text-slate-200">{t('manager_profile_access')}</div>
                        <div className="text-xs text-slate-500">{t('pin_required_desc')}</div>
                      </div>
                    </div>
                    <div className="space-y-2">
                      <label className="text-xs font-semibold text-slate-400 uppercase tracking-[0.2em]">
                        {(editingProfile as any).isManager ? t('pin_required') : t('pin_optional')}
                      </label>
                      <input
                        type="password"
                        inputMode="numeric"
                        value={(editingProfile as any).pin || ''}
                        onChange={e => {
                          const v = String(e.target.value || '').replace(/\D/g, '').slice(0, 6);
                          setEditingProfile({ ...(editingProfile || {}), pin: v });
                        }}
                        className="w-full bg-black/40 border border-white/10 rounded-xl px-4 py-2.5 text-sm text-white focus:ring-2 focus:ring-cyan-500 outline-none"
                        placeholder={t('pin_digits_placeholder')}
                      />
                      <div className="text-xs text-slate-500">
                        {(editingProfile as any).isManager ? t('pin_required_desc') : t('pin_optional_desc')}
                      </div>
                    </div>
                  </div>
                </div>
                <div className="flex justify-end space-x-3 mt-8">
                  <Button type="button" variant="ghost" onClick={() => setEditingProfile(null)}>{t('cancel')}</Button>
                  <Button type="submit" icon={<Save size={16} />}>{t('save_profile')}</Button>
                </div>
              </form>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                {profiles.map(p => (
                  <div key={p.id} className="bg-white/5 p-4 rounded-2xl border border-white/10 flex items-center justify-between group hover:border-cyan-400/40 hover:bg-white/10 transition-all">
                    <div className="flex items-center space-x-3">
                      <div className={`w-11 h-11 rounded-xl flex items-center justify-center overflow-hidden ${p.avatarColor}`}>{p.avatarImage ? <img src={p.avatarImage} alt={p.name} className="w-full h-full object-cover" /> : <span className="font-bold text-white">{p.name?.[0] || '?'}</span>}</div>
                      <div><h4 className="font-medium text-white">{p.name}</h4><p className="text-xs text-slate-500">{p.isKid ? t('kids') : t('adult')} • {p.language}</p></div>
                    </div>
                    <div className="flex space-x-1 opacity-0 group-hover:opacity-100"><button onClick={() => startEditProfile(p)} className="p-1.5 hover:bg-white/10 rounded text-slate-400"><Pencil size={14} /></button><button onClick={() => onDeleteProfile(p.id)} className="p-1.5 hover:bg-red-500/10 rounded text-slate-400 hover:text-red-400" disabled={profiles.length <= 1}><Trash2 size={14} /></button></div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-3 gap-8">
            {/* Left Column: Storage & Paths */}
            <div className="xl:col-span-2 space-y-8">
              <div className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0c1424] via-[#0d1626] to-[#0a0f1b] p-6 shadow-[0_20px_60px_-45px_rgba(15,23,42,0.9)]">
                <h2 className="text-xl font-semibold flex items-center mb-6 text-white"><HardDrive className="mr-3 text-cyan-400" /> {t('storage')}</h2>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {drives.map((d, i) => (
                    <div key={i} className="bg-white/5 p-5 rounded-2xl border border-white/10">
                      <div className="flex justify-between items-start mb-4"><div className="flex items-center"><div className="w-11 h-11 rounded-xl bg-gradient-to-br from-slate-700 to-slate-600 flex items-center justify-center mr-3 text-white font-bold">{String(d.path || d.label || '?').replace(':', '')}</div><div><p className="font-medium text-white">{d.label || t('local_disk')}</p><p className="text-xs text-slate-400 font-mono">{d.path}</p></div></div><div className="text-right"><p className="text-sm font-bold text-white">{Math.round((d.freeSpace || 0) / 1073741824)} GB</p><p className="text-[10px] text-slate-500 uppercase">{t('free_space')}</p></div></div>
                      <div className="h-2 rounded-full bg-white/5 overflow-hidden"><div style={{ width: `${(d.totalSpace ? ((d.totalSpace - (d.freeSpace || 0)) / d.totalSpace) * 100 : 0)}%` }} className="h-full bg-gradient-to-r from-cyan-400 to-sky-500" /></div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0b1220] via-[#0f172a] to-[#0b1220] p-6 shadow-[0_20px_60px_-45px_rgba(15,23,42,0.9)]">
                <h2 className="text-xl font-semibold flex items-center mb-6 text-white"><FolderPlusIcon className="mr-3 text-cyan-400" /> {t('library_folders')}</h2>
                <form onSubmit={handleAddPath} className="flex flex-wrap gap-3 mb-6"><input type="text" value={newPathInput} onChange={e => setNewPathInput(e.target.value)} placeholder={t('library_path_placeholder')} className="flex-1 min-w-[220px] bg-black/40 border border-white/10 rounded-xl px-4 py-2.5 text-sm text-white focus:ring-2 focus:ring-cyan-500 outline-none" /><Button type="button" variant="ghost" onClick={() => setShowFolderPicker(true)} icon={<FolderPlus size={16} />}>{t('browse')}</Button><Button type="submit" disabled={!newPathInput.trim()} icon={<Plus size={16} />}>{t('add')}</Button></form>
                <FolderPickerModal open={showFolderPicker} onClose={() => setShowFolderPicker(false)} onConfirm={paths => { for (const p of paths || []) onAddPath(p, newPathType); setShowFolderPicker(false); }} />
                <div className="space-y-2">
                  {libraryPaths.map((p) => (
                    <div key={p} className="flex items-center justify-between p-4 bg-white/5 rounded-2xl border border-white/10 group hover:border-cyan-400/40 hover:bg-white/10 transition-all">
                      <div className="flex items-center min-w-0"><FolderPlusIcon size={16} className="text-slate-400 mr-3" /><span className="text-sm text-slate-200 font-mono truncate">{p}</span></div>
                      <button onClick={() => onRemovePath(p)} className="text-slate-500 hover:text-red-400 p-2"><Trash2 size={16} /></button>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* Right Column: Scan & Metadata Dashboard */}
            <div className="space-y-8">
          <SystemStatusDashboard
            scanStatus={scanStatus}
            enrich={enrich}
            migrationRunning={!!migrationRunning}
            isAdmin={true}
            activeProviderLabel={currentApiProvider}
            configuredAnimeProvider={configuredAnimeProvider}
            scanItemsTotal={scanItemsTotal}
            onStartScan={onStartScan}
            onStartEnrich={onStartEnrich}
            onRetryNoMatch={handleRetryNoMatch}
            onRetryError={handleRetryError}
            retryMode={retryProviderMode}
            onChangeRetryMode={setRetryProviderMode}
            setShowSystemLogs={setShowSystemLogs}
            setShowNoMatchModal={setShowNoMatchModal}
          />
              <MetadataProviderConfig
                tmdbConfigured={tmdbConfigured} animeProvider={metadataConfig.animeProvider} kitsuStatus={kitsuHealth}
                showTmdbCredentials={showTmdbCredentials} setShowTmdbCredentials={setShowTmdbCredentials} setTmdbCredMsg={setTmdbCredMsg}
                showTmdbSecrets={showTmdbSecrets} setShowTmdbSecrets={setShowTmdbSecrets} tmdbApiKeyInput={tmdbApiKeyInput} setTmdbApiKeyInput={setTmdbApiKeyInput}
                tmdbAccessTokenInput={tmdbAccessTokenInput} setTmdbAccessTokenInput={setTmdbAccessTokenInput} tmdbUseV4={tmdbUseV4} setTmdbUseV4={setTmdbUseV4}
                tmdbBusy={tmdbBusy} handleValidateTmdbCredentials={handleValidateTmdbCredentials} handleSaveTmdbCredentials={handleSaveTmdbCredentials} tmdbCredMsg={tmdbCredMsg}
              />
              <ActivityFeed enrich={enrich} livePaused={livePaused} setLivePaused={setLivePaused} enrichHistory={enrichHistory} />
              <EnrichmentTuning
                enrich={enrich} enrichTuningBusy={enrichTuningBusy} handleSaveEnrichTuning={handleSaveEnrichTuning}
                enrichPreset={enrichPreset} setEnrichPreset={setEnrichPreset} applyEnrichPreset={applyEnrichPreset}
                jikanFetchEpisodes={jikanFetchEpisodes} setJikanFetchEpisodes={setJikanFetchEpisodes}
              />
            </div>
          </div>
        </div>
      </div>

      <div className={activeTab === 'control' ? 'block' : 'hidden'}>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Entity Search */}
          <div className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0b1020] via-[#0f172a] to-[#0b1020] p-6 shadow-[0_20px_60px_-45px_rgba(15,23,42,0.9)]">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold flex items-center text-white"><Database className="mr-3 text-amber-400" /> {t('control_data_title')}</h2>
              <Button size="sm" variant="ghost" onClick={() => void runControlSearch()} icon={<RefreshCw size={14} />}>{t('refresh')}</Button>
            </div>
            <div className="mb-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div className="rounded-2xl border border-white/10 bg-black/30 px-4 py-3">
                <div className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-500">Anime Mode</div>
                <div className="mt-1 text-sm font-semibold text-white">{configuredAnimeProvider || 'N/A'}</div>
              </div>
              <div className="rounded-2xl border border-cyan-500/15 bg-cyan-500/10 px-4 py-3">
                <div className="text-[10px] font-black uppercase tracking-[0.2em] text-cyan-200/70">Active API</div>
                <div className="mt-1 text-sm font-semibold text-cyan-100">{currentApiProvider || 'Idle'}</div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-black/30 px-4 py-3">
                <div className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-500">Kitsu API</div>
                <div className={`mt-1 text-sm font-semibold ${kitsuHealth?.ok ? 'text-emerald-300' : 'text-red-300'}`}>
                  {kitsuHealth == null ? 'Checking' : kitsuHealth.ok ? 'Online' : 'Unavailable'}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2 mb-3"><input type="text" value={controlQuery} onChange={e => setControlQuery(e.target.value)} className="flex-1 bg-black/40 border border-white/10 rounded-xl px-3 py-2 text-sm text-white focus:ring-2 focus:ring-amber-400 outline-none" placeholder={t('search_placeholder')} /><Button size="sm" onClick={() => void runControlSearch()}>{t('search')}</Button></div>
            <div className="space-y-2 max-h-72 overflow-auto">
              {controlResults.map(item => (
                <div key={String(item.id)} className="bg-white/5 p-3 rounded-xl border border-white/10 flex justify-between items-center">
                  <div className="min-w-0"><div className="text-sm text-slate-100 font-medium truncate">{item.title || `#${item.id}`}</div><div className="text-[11px] text-slate-500">TMDB: {item.tmdb_id || '-'}</div></div>
                  <div className="flex gap-2"><Button size="sm" variant="ghost" onClick={() => handleRematch(item)}>{t('control_rematch')}</Button><Button size="sm" variant="ghost" onClick={() => handleDeepPurge(Number(item.id))} icon={<Trash2 size={14} />} /></div>
                </div>
              ))}
            </div>
          </div>

          {/* Lenguaje / Anime */}
          <div className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0b1020] via-[#0f172a] to-[#0b1020] p-6 shadow-[0_20px_60px_-45px_rgba(15,23,42,0.9)]">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold flex items-center text-white">
                <Languages className="mr-3 text-amber-400" /> Lenguaje (Anime)
              </h2>
            </div>

            <div className="space-y-4">
                  <div className="bg-black/40 p-4 rounded-2xl border border-white/10">
                    <div className="flex items-center justify-between">
                      <div className="text-sm text-white font-semibold">Anime Normalize (Temporadas)</div>
                      <span className={`text-[10px] font-black uppercase tracking-widest px-2 py-1 rounded-full border ${anRunning ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-300' : 'bg-white/5 border-white/10 text-slate-400'}`}>
                        {anRunning ? 'Running' : 'Idle'}
                      </span>
                    </div>
                    <div className="mt-2 text-[11px] text-slate-400">
                      {(an as any).current_step ? String((an as any).current_step) : '—'} · {(an as any).current_title ? String((an as any).current_title) : '—'}
                    </div>
                    <div className="mt-3 h-2 rounded-full bg-white/5 overflow-hidden border border-white/10">
                      <div className="h-full bg-gradient-to-r from-amber-400 to-orange-500" style={{ width: `${anPct}%` }} />
                    </div>
                    <div className="mt-2 text-[11px] text-slate-500 font-mono">
                      {anProcessed}/{anTotal} · updated {(an as any).updated ?? 0} · failed {(an as any).failed ?? 0}
                    </div>
                    <div className="mt-3 flex gap-2">
                      <Button size="sm" onClick={() => void handleStartAnimeNormalize()} disabled={lenguajeBusy !== 'idle' || anRunning}>
                        Iniciar
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => void handleStopAnimeNormalize()} disabled={lenguajeBusy !== 'idle' || !anRunning}>
                        Detener
                      </Button>
                    </div>
                  </div>

                  <div className="bg-black/40 p-4 rounded-2xl border border-white/10">
                    <div className="flex items-center justify-between">
                      <div className="text-sm text-white font-semibold">Anime Translations (Jikan → ES)</div>
                      <span className={`text-[10px] font-black uppercase tracking-widest px-2 py-1 rounded-full border ${locRunning ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-300' : 'bg-white/5 border-white/10 text-slate-400'}`}>
                        {locRunning ? 'Running' : 'Idle'}
                      </span>
                    </div>
                    <div className="mt-2 text-[11px] text-slate-400">
                      {(loc as any).current_step ? String((loc as any).current_step) : '—'} · {(loc as any).current_title ? String((loc as any).current_title) : '—'}
                    </div>
                    <div className="mt-3 h-2 rounded-full bg-white/5 overflow-hidden border border-white/10">
                      <div className="h-full bg-gradient-to-r from-cyan-400 to-blue-500" style={{ width: `${locPct}%` }} />
                    </div>
                    <div className="mt-2 text-[11px] text-slate-500 font-mono">
                      {locProcessed}/{locTotal} · failed {(loc as any).failed ?? 0}
                    </div>
                    <div className="mt-3 flex gap-2">
                      <Button size="sm" onClick={() => void handleRunAnimeTranslations()} disabled={lenguajeBusy !== 'idle'}>
                        Ejecutar traducciones
                      </Button>
                    </div>
                  </div>
                </div>
          </div>
          {/* Telemetry */}
          <div className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0b1020] via-[#0f172a] to-[#0b1020] p-6 shadow-[0_20px_60px_-45px_rgba(15,23,42,0.9)] lg:col-span-2">
            <h2 className="text-xl font-semibold flex items-center text-white mb-4"><ScanLine className="mr-3 text-amber-400" /> {t('control_telemetry')}</h2>
            <div className="mb-4 grid grid-cols-1 md:grid-cols-4 gap-3">
              <div className="rounded-2xl border border-white/10 bg-black/30 px-4 py-3">
                <div className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-500">Live API</div>
                <div className="mt-1 text-sm font-semibold text-white">{currentApiProvider || 'Idle'}</div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-black/30 px-4 py-3">
                <div className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-500">Jikan</div>
                <div className={`mt-1 text-sm font-semibold ${jikanHealth?.ok ? 'text-emerald-300' : 'text-red-300'}`}>{jikanHealth == null ? 'Checking' : jikanHealth.ok ? 'Online' : 'Unavailable'}</div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-black/30 px-4 py-3">
                <div className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-500">Kitsu</div>
                <div className={`mt-1 text-sm font-semibold ${kitsuHealth?.ok ? 'text-emerald-300' : 'text-red-300'}`}>{kitsuHealth == null ? 'Checking' : kitsuHealth.ok ? 'Online' : 'Unavailable'}</div>
              </div>
              <div className="rounded-2xl border border-white/10 bg-black/30 px-4 py-3">
                <div className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-500">API Events</div>
                <div className="mt-1 text-sm font-semibold text-white">{recentApiActivity.length}</div>
              </div>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div className="space-y-2">
                {telemetryTaskList.map(task => (
                  <div key={task.task_id} className="bg-black/40 p-3 rounded-xl border border-white/10">
                    <div className="flex justify-between mb-2"><div className="text-sm text-slate-200 font-medium">{task.title || task.task_id}</div><div className="text-[11px] text-slate-500">{task.current_phase || task.status}</div></div>
                    <div className="flex flex-wrap gap-2">{TELEMETRY_PHASES.map(p => <span key={p} className={`px-2 py-0.5 rounded-full text-[10px] ${task.phases?.[p]?.status === 'done' ? 'bg-green-500/20 text-green-300' : task.phases?.[p]?.status === 'running' ? 'bg-blue-500/20 text-blue-300' : 'bg-slate-800 text-slate-400'}`}>{p.replace('_', ' ')}</span>)}</div>
                  </div>
                ))}
                {recentApiActivity.map((ev, idx) => (
                  <div key={`api-${idx}`} className="bg-black/30 p-3 rounded-xl border border-cyan-500/10">
                    <div className="flex items-center justify-between gap-3">
                      <div className="text-sm font-semibold text-cyan-100">{String(ev.provider || ev.service || 'unknown').toUpperCase()}</div>
                      <div className="text-[11px] text-slate-500">{ev.status_code ? `HTTP ${ev.status_code}` : ev.type}</div>
                    </div>
                    <div className="mt-1 text-[11px] text-slate-400 font-mono truncate">{String(ev.endpoint || ev.message || 'activity')}</div>
                  </div>
                ))}
              </div>
              <div className="min-h-[420px] max-h-[56vh] overflow-auto bg-black/40 p-3 rounded-xl border border-white/10 text-xs">
                {filteredLogs.map((ev, idx) => (
                  <div key={idx} className="py-1 border-b border-white/5 last:border-b-0">
                    <span className="text-slate-500 mr-2">{new Date((ev.ts || 0) * 1000).toLocaleTimeString()}</span>
                    <span className={`mr-2 ${String(ev.level).toUpperCase().includes('ERROR') ? 'text-red-300' : 'text-slate-300'}`}>[{String(ev.level).toUpperCase()}]</span>
                    <span className="text-slate-200">{ev.message}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>

      <HelpModal open={showHelpModal} onClose={() => setShowHelpModal(false)} />
      <NoMatchModal
        open={showNoMatchModal}
        onClose={() => setShowNoMatchModal(false)}
        onChanged={() => {
          if (onRefresh) onRefresh();
        }}
      />
    </div>
  );
};
