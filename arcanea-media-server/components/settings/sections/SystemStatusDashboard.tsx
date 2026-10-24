import React from 'react';
import { ScanLine, RefreshCw, HardDrive, Database, Loader2, AlertCircle } from 'lucide-react';
import { Button } from '../../ui/Button';
import { ScanStatus } from '../../../types';
import { useI18n } from '../../../i18n/i18n';

interface SystemStatusDashboardProps {
  scanStatus: ScanStatus;
  enrich: any;
  migrationRunning: boolean;
  isAdmin: boolean;
  activeProviderLabel?: string | null;
  configuredAnimeProvider?: string | null;
  scanItemsTotal: number;
  onStartScan: () => void;
  onStartEnrich?: () => void;
  onRetryNoMatch?: () => void;
  onRetryError?: () => void;
  retryMode?: 'auto' | 'jikan' | 'kitsu' | 'tmdb';
  onChangeRetryMode?: (mode: 'auto' | 'jikan' | 'kitsu' | 'tmdb') => void;
  setShowSystemLogs: (show: boolean) => void;
  setShowNoMatchModal: (show: boolean) => void;
}

export const SystemStatusDashboard: React.FC<SystemStatusDashboardProps> = ({
  scanStatus,
  enrich,
  migrationRunning,
  isAdmin,
  activeProviderLabel,
  configuredAnimeProvider,
  scanItemsTotal,
  onStartScan,
  onStartEnrich,
  onRetryNoMatch,
  onRetryError,
  retryMode,
  onChangeRetryMode,
  setShowSystemLogs,
  setShowNoMatchModal,
}) => {
  const { t } = useI18n();

  const clampPct = (v: number) => {
    if (!Number.isFinite(v)) return 0;
    if (v < 0) return 0;
    if (v > 100) return 100;
    return v;
  };

  const parseStepProgress = (rawStep: any): { done: number; total: number } | null => {
    const s = String(rawStep || '');
    const m = s.match(/(\d+)\s*\/\s*(\d+)/);
    if (!m) return null;
    const done = Number(m[1]);
    const total = Number(m[2]);
    if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return null;
    return { done, total };
  };

  const getScanCounts = () => {
    try {
      const processed = (scanStatus as any).processed ?? (scanStatus as any).processedFiles;
      const total = (scanStatus as any).total ?? (scanStatus as any).totalFiles;
      return {
        processed: typeof processed === 'number' ? processed : undefined,
        total: typeof total === 'number' ? total : undefined,
      };
    } catch {
      return { processed: undefined, total: undefined };
    }
  };

  const computeEnrichProgress = (job: any) => {
    try {
      const total = Number(job?.total ?? 0);
      const pendingTotal = (typeof job?.pending_total === 'number') ? Number(job.pending_total) : undefined;
      const pending = (typeof job?.pending === 'number') ? Number(job.pending) : undefined;
      if (total > 0 && (pendingTotal !== undefined || pending !== undefined)) {
        const left = pendingTotal !== undefined ? pendingTotal : (pending as number);
        return clampPct(((total - left) / total) * 100);
      }
    } catch {}
    const step = parseStepProgress(job?.current_step);
    if (step) return clampPct((step.done / step.total) * 100);
    return 0;
  };

  const formatEnrichStep = (rawStep: any) => {
    const s = typeof rawStep === 'string' ? rawStep : '';
    if (!s) return t('starting');
    return s;
  };

  const counts = getScanCounts();
  const enrichStep = String((enrich as any)?.current_step || '').toLowerCase();
  const ingestActive = !!(enrich && enrich.running) && (
    enrichStep.includes('commit') ||
    enrichStep.includes('db') ||
    enrichStep.includes('ingest') ||
    enrichStep.includes('save')
  );

  return (
    <div className="relative overflow-hidden rounded-3xl border border-white/10 bg-gradient-to-br from-slate-950/90 via-[#0f172a]/90 to-slate-900/80 p-7 shadow-[0_20px_60px_rgba(0,0,0,0.35)]">
      {(scanStatus.scanning || (enrich && enrich.running)) && (
        <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-transparent via-indigo-500 to-transparent animate-shimmer" />
      )}

      <div className="flex items-start justify-between gap-4 mb-8">
        <div>
          <h2 className="text-2xl sm:text-3xl font-black flex items-center text-white tracking-tight">
            <ScanLine className={`mr-3 ${scanStatus.scanning || (enrich && enrich.running) ? 'text-indigo-400 animate-pulse' : 'text-slate-500'}`} size={28} />
            {t('system_status')}
          </h2>
          <p className="text-slate-400 text-xs mt-2 font-medium max-w-md">
            {scanStatus.scanning || (enrich && enrich.running) ? t('system_active_desc') : t('system_idle_desc')}
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant={scanStatus.scanning || (enrich && enrich.running) ? "ghost" : "primary"}
            onClick={() => onStartScan && onStartScan()}
            disabled={!!(scanStatus.scanning || (enrich && enrich.running)) || migrationRunning}
            icon={<RefreshCw size={16} />}
            className="rounded-xl font-bold"
          >
            {t('start_full_scan')}
          </Button>
        </div>
      </div>

      {/* High Level Stats Grid */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-8">
        <div className="rounded-2xl p-4 border border-white/5 bg-white/5 text-center">
          <span className="text-[10px] font-black text-slate-500 uppercase tracking-widest mb-1 block">{t('files_total')}</span>
          <span className="text-xl font-black text-white tabular-nums">{counts.total ?? '0'}</span>
        </div>
        <div className="rounded-2xl p-4 border border-white/5 bg-white/5 text-center">
          <span className="text-[10px] font-black text-indigo-400 uppercase tracking-widest mb-1 block">{t('titles_found')}</span>
          <span className="text-xl font-black text-white tabular-nums">{scanItemsTotal || (scanStatus as any).titles_total || '0'}</span>
        </div>
        <div className="rounded-2xl p-4 border border-white/5 bg-white/5 text-center">
          <span className="text-[10px] font-black text-emerald-400 uppercase tracking-widest mb-1 block">{t('enriched')}</span>
          <span className="text-xl font-black text-white tabular-nums">{enrich ? (enrich.total - (enrich.pending_total ?? enrich.pending ?? 0)) : '0'}</span>
        </div>
        <div className="rounded-2xl p-4 border border-white/5 bg-white/5 text-center">
          <span className="text-[10px] font-black text-amber-400 uppercase tracking-widest mb-1 block">{t('no_match')}</span>
          <span className="text-xl font-black text-white tabular-nums">{(enrich as any)?.no_match || '0'}</span>
        </div>
      </div>

      {/* Pipeline Summary */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
        <div className={`rounded-2xl border px-4 py-3 flex items-center justify-between ${scanStatus.scanning ? 'border-indigo-500/40 bg-indigo-500/10' : 'border-white/5 bg-white/5'}`}>
          <div>
            <div className="text-[10px] uppercase tracking-widest text-slate-400 font-black">{t('scanner_phase')}</div>
            <div className="text-xs text-slate-200">{scanStatus.scanning ? t('scanning_files') : t('scanner_idle')}</div>
          </div>
          <HardDrive size={18} className={scanStatus.scanning ? 'text-indigo-400' : 'text-slate-500'} />
        </div>
        <div className={`rounded-2xl border px-4 py-3 flex items-center justify-between ${ingestActive ? 'border-emerald-500/40 bg-emerald-500/10' : 'border-white/5 bg-white/5'}`}>
          <div>
            <div className="text-[10px] uppercase tracking-widest text-slate-400 font-black">{t('db_ingest')}</div>
            <div className="text-xs text-slate-200">{ingestActive ? t('db_ingest_running') : t('db_ingest_idle')}</div>
          </div>
          <Database size={18} className={ingestActive ? 'text-emerald-400' : 'text-slate-500'} />
        </div>
        <div className={`rounded-2xl border px-4 py-3 flex items-center justify-between ${(enrich && enrich.running) ? 'border-emerald-500/40 bg-emerald-500/10' : 'border-white/5 bg-white/5'}`}>
          <div>
            <div className="text-[10px] uppercase tracking-widest text-slate-400 font-black">{t('enrichment_phase')}</div>
            <div className="text-xs text-slate-200">{(enrich && enrich.running) ? t('fetching_metadata') : t('enrichment_idle')}</div>
          </div>
          <RefreshCw size={18} className={(enrich && enrich.running) ? 'text-emerald-400' : 'text-slate-500'} />
        </div>
      </div>

      {/* Unified Progress Visualizer */}
      <div className="space-y-6">
        {/* Scan Phase */}
        <div className={`p-5 rounded-2xl border transition-all duration-500 ${scanStatus.scanning ? 'bg-indigo-500/10 border-indigo-500/30 shadow-[0_0_30px_rgba(79,70,229,0.1)]' : 'bg-white/5 border-white/5 opacity-60'}`}>
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-3">
              <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${scanStatus.scanning ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/20' : 'bg-slate-800 text-slate-400'}`}>
                <HardDrive size={20} />
              </div>
              <div>
                <h3 className="text-sm font-black text-white uppercase tracking-tight">{t('scanner_phase')}</h3>
                <p className="text-[11px] text-slate-400 font-medium">
                  {scanStatus.scanning ? t('scanning_files') : t('scanner_idle')}
                </p>
              </div>
            </div>
            {scanStatus.scanning && (
              <div className="flex flex-col items-end">
                <span className="text-xs font-black text-indigo-400 tabular-nums">
                  {counts.processed} / {counts.total}
                </span>
                <span className="text-[10px] text-slate-500 font-bold uppercase tracking-widest">{t('files')}</span>
              </div>
            )}
          </div>
          
          {scanStatus.scanning && (
            <div className="space-y-3">
              <div className="h-2 w-full bg-black/40 rounded-full overflow-hidden">
                <div 
                  className="h-full bg-gradient-to-r from-indigo-600 to-indigo-400 shadow-[0_0_10px_rgba(79,70,229,0.5)] transition-all duration-700"
                  style={{ width: `${clampPct(((counts.processed || 0) / (counts.total || 1)) * 100)}%` }}
                />
              </div>
              <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-black/30 border border-white/5">
                <Loader2 className="animate-spin text-indigo-400" size={12} />
                <span className="text-[11px] text-slate-400 font-mono truncate flex-1">
                  {(scanStatus as any).current || scanStatus.currentFile || t('initializing')}
                </span>
              </div>
            </div>
          )}
        </div>

        {/* Enrichment Phase */}
        <div className={`p-5 rounded-2xl border transition-all duration-500 ${(enrich && enrich.running) ? 'bg-emerald-500/10 border-emerald-500/30 shadow-[0_0_30px_rgba(16,185,129,0.1)]' : 'bg-white/5 border-white/5 opacity-60'}`}>
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-3">
              <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${(enrich && enrich.running) ? 'bg-emerald-600 text-white shadow-lg shadow-emerald-600/20' : 'bg-slate-800 text-slate-400'}`}>
                <Database size={20} />
              </div>
              <div>
                <h3 className="text-sm font-black text-white uppercase tracking-tight">{t('enrichment_phase')}</h3>
                <p className="text-[11px] text-slate-400 font-medium">
                  {(enrich && enrich.running) ? t('fetching_metadata') : t('enrichment_idle')}
                </p>
              </div>
            </div>
            {(enrich && enrich.running) && (
              <div className="flex flex-col items-end">
                <span className="text-xs font-black text-emerald-400 tabular-nums">
                  {computeEnrichProgress(enrich).toFixed(1)}%
                </span>
                <span className="text-[10px] text-slate-500 font-bold uppercase tracking-widest">{t('metadata')}</span>
              </div>
            )}
          </div>

          {(enrich && enrich.running) && (
            <div className="space-y-3">
              <div className="h-2 w-full bg-black/40 rounded-full overflow-hidden">
                <div 
                  className="h-full bg-gradient-to-r from-emerald-600 to-emerald-400 shadow-[0_0_10px_rgba(16,185,129,0.5)] transition-all duration-700"
                  style={{ width: `${computeEnrichProgress(enrich)}%` }}
                />
              </div>
              <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-black/30 border border-white/5">
                <RefreshCw className="animate-spin text-emerald-400" size={12} />
                <div className="flex-1 min-w-0">
                  <div className="flex justify-between items-center">
                    <span className="text-[11px] text-slate-300 font-bold truncate">
                      {enrich.current_title || t('processing')}
                    </span>
                    <span className="text-[10px] text-slate-500 uppercase font-black ml-2 shrink-0">
                      {formatEnrichStep(enrich.current_step)}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {activeProviderLabel ? (
                      <span className="rounded-full border border-cyan-500/20 bg-cyan-500/10 px-2.5 py-1 text-[10px] font-black uppercase tracking-wider text-cyan-200">
                        API: {activeProviderLabel}
                      </span>
                    ) : null}
                    {configuredAnimeProvider ? (
                      <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-[10px] font-black uppercase tracking-wider text-slate-300">
                        Anime mode: {configuredAnimeProvider}
                      </span>
                    ) : null}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Action Bar */}
      <div className="mt-8 pt-6 border-t border-white/5 flex flex-wrap gap-3">
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setShowSystemLogs(true)}
          icon={<ScanLine size={16} />}
          className="rounded-xl"
        >
          {t('view_logs')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setShowNoMatchModal(true)}
          disabled={!((enrich as any)?.no_match > 0)}
          icon={<AlertCircle size={16} />}
          className="rounded-xl"
        >
          {t('review_no_match')} ({ (enrich as any)?.no_match || 0 })
        </Button>
        {isAdmin && (enrich as any)?.no_match > 0 && (
          <div className="flex items-center gap-2">
            <select
              value={(retryMode || 'auto')}
              onChange={(e) => onChangeRetryMode && onChangeRetryMode((e.target.value as any) || 'auto')}
              disabled={!!(enrich && enrich.running) || scanStatus.scanning}
              className="h-9 px-3 rounded-xl bg-black/30 border border-white/10 text-xs text-slate-200 focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
              title="Retry provider mode"
            >
              <option value="auto">AUTO</option>
              <option value="jikan">JIKAN</option>
              <option value="kitsu">KITSU</option>
              <option value="tmdb">TMDB</option>
            </select>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onRetryNoMatch && onRetryNoMatch()}
              disabled={!!(enrich && enrich.running) || scanStatus.scanning}
              icon={<RefreshCw size={16} />}
              className="rounded-xl"
            >
              {t('retry_no_match')} ({(enrich as any).no_match})
            </Button>
            {!!(enrich as any)?.error && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onRetryError && onRetryError()}
                disabled={!!(enrich && enrich.running) || scanStatus.scanning}
                icon={<RefreshCw size={16} />}
                className="rounded-xl"
              >
                Retry ERROR ({(enrich as any).error})
              </Button>
            )}
          </div>
        )}
        {isAdmin && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onStartEnrich && onStartEnrich()}
            disabled={!!(enrich && enrich.running) || scanStatus.scanning}
            icon={<RefreshCw size={16} />}
            className="rounded-xl"
          >
            {t('force_enrichment')}
          </Button>
        )}
      </div>
    </div>
  );
};
