import React from 'react';
import { Database, Pencil, X, Loader2, CheckCircle, Save, Film } from 'lucide-react';
import { Button } from '../../ui/Button';
import { useI18n } from '../../../i18n/i18n';

interface MetadataProviderConfigProps {
  tmdbConfigured: boolean | null;
  animeProvider: 'jikan' | 'kitsu' | 'tmdb' | 'auto';
  kitsuStatus?: { ok?: boolean; detail?: string | null; cached?: boolean } | null;
  showTmdbCredentials: boolean;
  setShowTmdbCredentials: (show: boolean) => void;
  setTmdbCredMsg: (msg: string | null) => void;
  showTmdbSecrets: boolean;
  setShowTmdbSecrets: (show: boolean) => void;
  tmdbApiKeyInput: string;
  setTmdbApiKeyInput: (val: string) => void;
  tmdbAccessTokenInput: string;
  setTmdbAccessTokenInput: (val: string) => void;
  tmdbUseV4: boolean;
  setTmdbUseV4: (val: boolean) => void;
  tmdbBusy: 'idle' | 'validating' | 'saving';
  handleValidateTmdbCredentials: () => void;
  handleSaveTmdbCredentials: () => void;
  tmdbCredMsg: string | null;
}

export const MetadataProviderConfig: React.FC<MetadataProviderConfigProps> = ({
  tmdbConfigured,
  animeProvider,
  kitsuStatus,
  showTmdbCredentials,
  setShowTmdbCredentials,
  setTmdbCredMsg,
  showTmdbSecrets,
  setShowTmdbSecrets,
  tmdbApiKeyInput,
  setTmdbApiKeyInput,
  tmdbAccessTokenInput,
  setTmdbAccessTokenInput,
  tmdbUseV4,
  setTmdbUseV4,
  tmdbBusy,
  handleValidateTmdbCredentials,
  handleSaveTmdbCredentials,
  tmdbCredMsg,
}) => {
  const { t } = useI18n();
  const kitsuOk = kitsuStatus ? !!kitsuStatus.ok : null;
  const animeModeLabel = animeProvider === 'auto' ? 'Auto' : String(animeProvider || '').toUpperCase();

  return (
    <div className="rounded-3xl border border-white/10 bg-gradient-to-br from-[#0b1220] via-[#0f172a] to-[#0b1220] p-6 shadow-[0_20px_60px_-45px_rgba(15,23,42,0.9)]">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-semibold flex items-center text-white">
          <Database className="mr-3 text-cyan-400" /> {t('metadata_providers')}
        </h2>
        <div className="flex items-center gap-2">
          <span className="rounded-full border border-cyan-500/20 bg-cyan-500/10 px-3 py-1 text-[10px] font-black uppercase tracking-[0.2em] text-cyan-200">
            Anime: {animeModeLabel}
          </span>
          <span className="text-xs text-slate-400 uppercase tracking-[0.2em]">TMDB</span>
          {tmdbConfigured === null ? (
            <span className="text-xs text-slate-500 animate-pulse">{t('checking')}</span>
          ) : tmdbConfigured ? (
            <span className="px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-300 text-[10px] font-black border border-emerald-500/20 uppercase tracking-wider">{t('configured')}</span>
          ) : (
            <span className="px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-300 text-[10px] font-black border border-amber-500/20 uppercase tracking-wider">{t('not_configured')}</span>
          )}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setShowTmdbCredentials(!showTmdbCredentials);
              setTmdbCredMsg(null);
            }}
            icon={showTmdbCredentials ? <X size={14} /> : <Pencil size={14} />}
            className="ml-2 rounded-xl"
          >
            {showTmdbCredentials ? t('close') : t('configure')}
          </Button>
        </div>
      </div>

      {showTmdbCredentials && (
        <div className="mb-6 animate-in slide-in-from-top-4 duration-300">
          <div className="bg-black/40 border border-white/10 rounded-2xl p-6 shadow-xl">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-black text-white uppercase tracking-widest">{t('tmdb_credentials')}</h3>
              <label className="flex items-center gap-2 text-xs font-bold text-slate-500 cursor-pointer hover:text-slate-300 transition-colors">
                <input
                  type="checkbox"
                  checked={showTmdbSecrets}
                  onChange={(e) => setShowTmdbSecrets(e.target.checked)}
                  className="rounded border-slate-700 bg-slate-800 text-indigo-500 focus:ring-indigo-500"
                />
                {t('show_secrets')}
              </label>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="space-y-2">
                <label className="block text-[10px] font-black text-slate-500 uppercase tracking-[0.2em]">{t('tmdb_api_key')}</label>
                <input
                  type={showTmdbSecrets ? 'text' : 'password'}
                  value={tmdbApiKeyInput}
                  onChange={(e) => setTmdbApiKeyInput(e.target.value)}
                  className="w-full bg-black/40 border border-white/10 rounded-xl px-4 py-3 text-sm text-white focus:ring-2 focus:ring-cyan-500 outline-none transition-all"
                  placeholder="v3 API Key"
                />
              </div>
              <div className="space-y-2">
                <label className="block text-[10px] font-black text-slate-500 uppercase tracking-[0.2em]">{t('tmdb_access_token')}</label>
                <input
                  type={showTmdbSecrets ? 'text' : 'password'}
                  value={tmdbAccessTokenInput}
                  onChange={(e) => setTmdbAccessTokenInput(e.target.value)}
                  className="w-full bg-black/40 border border-white/10 rounded-xl px-4 py-3 text-sm text-white focus:ring-2 focus:ring-cyan-500 outline-none transition-all"
                  placeholder="v4 Access Token"
                />
              </div>
            </div>

            <div className="mt-6 flex flex-wrap items-center justify-between gap-4">
              <label className="flex items-center gap-3 cursor-pointer group">
                <div className={`w-10 h-5 rounded-full p-1 transition-all duration-300 ${tmdbUseV4 ? 'bg-cyan-500' : 'bg-slate-700'}`}>
                  <div className={`w-3 h-3 bg-white rounded-full shadow-md transition-transform duration-300 ${tmdbUseV4 ? 'translate-x-5' : ''}`} />
                </div>
                <input
                  type="checkbox"
                  className="hidden"
                  checked={tmdbUseV4}
                  onChange={(e) => setTmdbUseV4(e.target.checked)}
                />
                <span className="text-xs font-bold text-slate-400 group-hover:text-slate-200 transition-colors">{t('use_v4_token')}</span>
              </label>

              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={handleValidateTmdbCredentials}
                  disabled={tmdbBusy !== 'idle' || (!tmdbApiKeyInput.trim() && !tmdbAccessTokenInput.trim())}
                  icon={tmdbBusy === 'validating' ? <Loader2 className="animate-spin" size={14} /> : <CheckCircle size={14} />}
                  className="rounded-xl"
                >
                  {t('validate')}
                </Button>
                <Button
                  size="sm"
                  onClick={handleSaveTmdbCredentials}
                  disabled={tmdbBusy !== 'idle' || (!tmdbApiKeyInput.trim() && !tmdbAccessTokenInput.trim() && !tmdbUseV4)}
                  icon={tmdbBusy === 'saving' ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
                  className="rounded-xl"
                >
                  {t('save')}
                </Button>
              </div>
            </div>

            {tmdbCredMsg && (
              <div className={`mt-4 p-3 rounded-xl text-xs font-bold border animate-in zoom-in-95 duration-300 ${tmdbCredMsg.toLowerCase().includes('fail') || tmdbCredMsg.toLowerCase().includes('error') ? 'bg-red-500/10 text-red-400 border-red-500/20' : 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'}`}>
                {tmdbCredMsg}
              </div>
            )}
          </div>
        </div>
      )}

      <div className="space-y-4">
        <div className="flex items-center justify-between p-4 bg-white/5 rounded-2xl border border-white/5">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-500/10 flex items-center justify-center text-indigo-400">
              <Database size={20} />
            </div>
            <div>
              <h3 className="text-sm font-bold text-white">Jikan (MAL)</h3>
              <p className="text-[11px] text-slate-500 font-medium">Provider para Anime</p>
            </div>
          </div>
          <span className={`px-2 py-0.5 rounded-full text-[10px] font-black border uppercase tracking-wider ${animeProvider === 'jikan' || animeProvider === 'auto' ? 'bg-emerald-500/10 text-emerald-300 border-emerald-500/20' : 'bg-slate-500/10 text-slate-400 border-slate-500/20'}`}>
            {animeProvider === 'jikan' || animeProvider === 'auto' ? t('active') : 'Standby'}
          </span>
        </div>

        <div className="flex items-center justify-between p-4 bg-white/5 rounded-2xl border border-white/10">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-cyan-500/10 flex items-center justify-center text-cyan-300">
              <Database size={20} />
            </div>
            <div>
              <h3 className="text-sm font-bold text-white">Kitsu</h3>
              <p className="text-[11px] text-slate-500 font-medium">API alternativa para Anime</p>
              {kitsuStatus?.detail ? (
                <p className="mt-1 text-[10px] text-slate-500 line-clamp-1">{kitsuStatus.detail}</p>
              ) : null}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {kitsuOk === null ? (
              <span className="px-2 py-0.5 rounded-full bg-slate-500/10 text-slate-400 text-[10px] font-black border border-slate-500/20 uppercase tracking-wider">{t('checking')}</span>
            ) : kitsuOk ? (
              <span className="px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-300 text-[10px] font-black border border-emerald-500/20 uppercase tracking-wider">API OK</span>
            ) : (
              <span className="px-2 py-0.5 rounded-full bg-red-500/10 text-red-300 text-[10px] font-black border border-red-500/20 uppercase tracking-wider">API Down</span>
            )}
            <span className={`px-2 py-0.5 rounded-full text-[10px] font-black border uppercase tracking-wider ${animeProvider === 'kitsu' ? 'bg-cyan-500/10 text-cyan-200 border-cyan-500/20' : 'bg-slate-500/10 text-slate-400 border-slate-500/20'}`}>
              {animeProvider === 'kitsu' ? 'Selected' : 'Standby'}
            </span>
          </div>
        </div>
        
        <div className="flex items-center justify-between p-4 bg-white/5 rounded-2xl border border-white/10">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-500/10 flex items-center justify-center text-indigo-400">
              <Film size={20} />
            </div>
            <div>
              <h3 className="text-sm font-bold text-white">TMDB</h3>
              <p className="text-[11px] text-slate-500 font-medium">Provider para Películas y Series</p>
            </div>
          </div>
          {tmdbConfigured ? (
            <span className="px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-300 text-[10px] font-black border border-emerald-500/20 uppercase tracking-wider">{t('active')}</span>
          ) : (
            <span className="px-2 py-0.5 rounded-full bg-slate-500/10 text-slate-400 text-[10px] font-black border border-slate-500/20 uppercase tracking-wider">{t('not_configured')}</span>
          )}
        </div>
      </div>
    </div>
  );
};
