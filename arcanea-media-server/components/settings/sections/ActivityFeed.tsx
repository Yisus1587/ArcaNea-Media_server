import React from 'react';
import { RefreshCw, CheckCircle } from 'lucide-react';
import { useI18n } from '../../../i18n/i18n';

interface ActivityFeedProps {
  enrich: any;
  livePaused: boolean;
  setLivePaused: (paused: boolean | ((prev: boolean) => boolean)) => void;
  enrichHistory: any[];
}

export const ActivityFeed: React.FC<ActivityFeedProps> = ({
  enrich,
  livePaused,
  setLivePaused,
  enrichHistory,
}) => {
  const { t } = useI18n();

  return (
    <div className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0b1220] via-[#0f172a] to-[#0b1220] p-6 shadow-[0_20px_60px_-45px_rgba(15,23,42,0.9)]">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-semibold flex items-center text-white">
          <RefreshCw className={`mr-3 ${enrich && enrich.running ? 'animate-spin text-cyan-400' : 'text-slate-500'}`} size={20} />
          {t('recent_activity')}
        </h2>
        <button 
          onClick={() => setLivePaused(p => !p)} 
          className={`px-3 py-1 text-[10px] font-black uppercase tracking-widest rounded-full border transition-all ${livePaused ? 'bg-amber-500/10 border-amber-500/20 text-amber-300' : 'bg-white/5 border-white/10 text-slate-400 hover:bg-white/10'}`}
        >
          {livePaused ? t('resume') : t('pause')}
        </button>
      </div>

      <div className="space-y-3 max-h-[400px] overflow-y-auto pr-2 custom-scrollbar">
        {enrichHistory.length === 0 ? (
          <div className="text-center py-12 bg-white/5 rounded-2xl border border-dashed border-white/10">
            <RefreshCw className="mx-auto h-8 w-8 text-slate-700 mb-3" />
            <p className="text-xs text-slate-500 font-bold uppercase tracking-widest">{t('no_recent_events')}</p>
          </div>
        ) : (
          enrichHistory.map((ev, i) => (
            <div key={i} className="flex items-center justify-between p-3 rounded-2xl bg-white/5 border border-white/10 hover:bg-white/10 transition-colors animate-in slide-in-from-right-2 duration-300">
              <div className="flex items-center gap-3">
                <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${ev.type === 'processing' ? 'bg-cyan-500/20 text-cyan-400' : 'bg-emerald-500/20 text-emerald-400'}`}>
                  {ev.type === 'processing' ? <RefreshCw size={14} className="animate-spin" /> : <CheckCircle size={14} />}
                </div>
                <div>
                  <div className="text-xs font-bold text-white">
                    {ev.type === 'processing' ? `${t('processing')}: ${ev.title || ('#' + ev.id)}` : ev.type === 'ingested' ? `${t('ingested')} ${ev.count} ${t('items')}` : String(ev.type)}
                  </div>
                  <div className="text-[10px] text-slate-500 font-medium mt-0.5">{new Date(ev.ts).toLocaleTimeString()}</div>
                </div>
              </div>
              {ev.id && <div className="text-[10px] font-mono text-slate-600 bg-white/5 px-2 py-1 rounded">#{ev.id}</div>}
            </div>
          ))
        )}
      </div>
    </div>
  );
};
