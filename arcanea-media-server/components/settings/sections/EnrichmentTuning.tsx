import React from 'react';
import { Wrench, Loader2, Save } from 'lucide-react';
import { Button } from '../../ui/Button';
import { useI18n } from '../../../i18n/i18n';

interface EnrichmentTuningProps {
  enrich: any;
  enrichTuningBusy: boolean;
  handleSaveEnrichTuning: () => void;
  enrichPreset: 'safe' | 'balanced' | 'fast';
  setEnrichPreset: (val: 'safe' | 'balanced' | 'fast') => void;
  applyEnrichPreset: (preset: 'safe' | 'balanced' | 'fast') => void;
  jikanFetchEpisodes: boolean;
  setJikanFetchEpisodes: (val: boolean) => void;
}

export const EnrichmentTuning: React.FC<EnrichmentTuningProps> = ({
  enrich,
  enrichTuningBusy,
  handleSaveEnrichTuning,
  enrichPreset,
  setEnrichPreset,
  applyEnrichPreset,
  jikanFetchEpisodes,
  setJikanFetchEpisodes,
}) => {
  const { t } = useI18n();

  return (
    <div className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0b1220] via-[#0f172a] to-[#0b1220] p-6 shadow-[0_20px_60px_-45px_rgba(15,23,42,0.9)]">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-bold text-white flex items-center gap-3">
          <Wrench size={18} className="text-cyan-400" />
          {t('enrich_tuning_title')}
        </h2>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => void handleSaveEnrichTuning()}
          disabled={enrichTuningBusy || !!(enrich && enrich.running)}
          icon={enrichTuningBusy ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
          className="rounded-xl"
        >
          {t('save_tuning')}
        </Button>
      </div>
      
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="space-y-4">
          <div className="flex items-center gap-4">
            <select
              className="flex-1 bg-black/40 border border-white/10 rounded-xl px-4 py-2.5 text-sm text-white focus:ring-2 focus:ring-cyan-500 outline-none"
              value={enrichPreset}
              onChange={(e) => setEnrichPreset(e.target.value as any)}
              disabled={!!(enrich && enrich.running)}
            >
              <option value="safe">{t('enrich_preset_safe')}</option>
              <option value="balanced">{t('enrich_preset_balanced')}</option>
              <option value="fast">{t('enrich_preset_fast')}</option>
            </select>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => void applyEnrichPreset(enrichPreset)}
              disabled={enrichTuningBusy || !!(enrich && enrich.running)}
              className="rounded-xl"
            >
              {t('apply')}
            </Button>
          </div>
          <p className="text-[11px] text-slate-500 font-medium leading-relaxed">
            {t('enrich_tuning_desc')}
          </p>
        </div>

        <div className="space-y-4">
          <div className="flex items-center justify-between p-3 rounded-2xl bg-white/5 border border-white/10">
            <div className="flex flex-col">
              <span className="text-xs font-bold text-slate-300">{t('fetch_episodes')}</span>
              <span className="text-[10px] text-slate-500">{t('jikan_episodes_desc')}</span>
            </div>
            <button 
              onClick={() => setJikanFetchEpisodes(!jikanFetchEpisodes)}
              className={`w-10 h-5 rounded-full p-1 transition-all duration-300 ${jikanFetchEpisodes ? 'bg-cyan-500' : 'bg-slate-700'}`}
            >
              <div className={`w-3 h-3 bg-white rounded-full shadow-md transition-transform duration-300 ${jikanFetchEpisodes ? 'translate-x-5' : ''}`} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
