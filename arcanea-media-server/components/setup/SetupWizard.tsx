import React, { useEffect, useState } from 'react';
import { UserProfile, MetadataConfig } from '../../types';
import { Button } from '../ui/Button';
import { MediaService, setAdminMode } from '../../services/api';
import { ChevronRight, FolderPlus, Database, User, Check, HardDrive, Trash2, ArrowRight } from 'lucide-react';
import { detectSystemLocale, detectSystemUiLang, getStoredUiLang, getStoredUiLangSource, normalizeMetadataLanguageFromLocale, uiLangFromLocale, useI18n } from '../../i18n/i18n';
import { FolderPickerModal } from '../settings/FolderPickerModal';

interface SetupWizardProps {
  onComplete: (profile: UserProfile, paths: string[], metadata: MetadataConfig) => void;
}

type RootTypeChoice = 'auto' | 'movie' | 'series' | 'anime';

const AVATAR_COLORS = [
  'bg-indigo-600', 'bg-purple-600', 'bg-pink-600', 'bg-red-600', 
  'bg-orange-600', 'bg-yellow-500', 'bg-green-600', 'bg-blue-600', 'bg-slate-600'
];

export const SetupWizard: React.FC<SetupWizardProps> = ({ onComplete }) => {
  const { setLang, t } = useI18n();
  const systemUiLang = detectSystemUiLang(); // 'es' | 'en'
  const storedUiLang = getStoredUiLang();
  const initialUiLang = storedUiLang ?? systemUiLang;
  const defaultUiLocale = initialUiLang === 'es' ? 'es-419' : 'en-US';
  const defaultProviderLanguage = normalizeMetadataLanguageFromLocale(detectSystemLocale());
  const [step, setStep] = useState(1);
  
   // Paso 1: Perfil
  const [managerName, setManagerName] = useState('');
  const [managerLanguage, setManagerLanguage] = useState(defaultUiLocale);
  const [managerPin, setManagerPin] = useState('');
  const [pinRequired, setPinRequired] = useState(true);
  const [langTouched, setLangTouched] = useState(false);
  
   // Paso 2: Bibliotecas
  const [paths, setPaths] = useState<string[]>([]);
  const [pathTypes, setPathTypes] = useState<Record<string, RootTypeChoice>>({});
  const [pathInput, setPathInput] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);
  const [tmdbLocalization, setTmdbLocalization] = useState<boolean>(systemUiLang === 'es');

   // Paso 3: Metadatos
  const [metadata, setMetadata] = useState<MetadataConfig>({
    moviesProvider: 'tmdb',
    animeProvider: 'kitsu',
    language: defaultProviderLanguage,
    downloadImages: true,
    fetchCast: true
  });
   // TMDB API key (opcional)
  const [tmdbApiKey, setTmdbApiKey] = useState('');
  const [showTmdbKey, setShowTmdbKey] = useState(false);
  const [tmdbStatusMsg, setTmdbStatusMsg] = useState<string | null>(null);
   const [tmdbChecking, setTmdbChecking] = useState(false);

  const handleNext = async () => {
    if (step === 1) {
      const pinRaw = String(managerPin || '').replace(/\D/g, '');
      const pin = pinRaw ? pinRaw.slice(0, 6) : '';
      if (pinRequired) {
        if (!pin || pin.length < 4) {
          window.alert(t('manager_pin_required_alert'));
          return;
        }
        const res = await MediaService.adminLogin(pin);
        if (!res?.ok) {
          window.alert(t('pin_incorrect'));
          return;
        }
      }
    }
    setStep(prev => prev + 1);
  };

   // Alinear idioma UI con el sistema al iniciar (no sobrescribe elección manual).
  useEffect(() => {
    // Respect explicit user choice if it already exists.
    const stored = getStoredUiLang();
    const source = getStoredUiLangSource();
    if (!stored && source !== 'manual') setLang(systemUiLang, 'system');
    (async () => {
      try {
        const status = await MediaService.getAdminStatus();
        if (typeof status?.pin_required === 'boolean') {
          setPinRequired(status.pin_required);
          if (!status.pin_required) {
            // Allow admin headers during bootstrap when no PIN is set yet.
            setAdminMode(true);
          }
        }
      } catch {
        // ignore
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

   // Al cambiar idioma en onboarding, sincronizar i18n y lenguaje proveedor.
  useEffect(() => {
    const ui = uiLangFromLocale(managerLanguage);
    const stored = getStoredUiLang();
    const source = getStoredUiLangSource();
    const effectiveSource = langTouched ? 'manual' : ((source === 'manual' || stored) ? 'manual' : 'system');
    setLang(ui, effectiveSource);
    setMetadata((m) => ({ ...m, language: normalizeMetadataLanguageFromLocale(managerLanguage) }));
  }, [managerLanguage, langTouched, setLang]);

  const inferRootTypeForPath = (path: string): RootTypeChoice => {
    const last = String(path || '').split(/[\\/]+/).filter(Boolean).slice(-1)[0] || '';
    const s = last.toLowerCase();
    if (/(^|[^a-z])anime([^a-z]|$)/.test(s) || /animes?/.test(s)) return 'anime';
    if (/(^|[^a-z])(series|serie|tv|shows?)([^a-z]|$)/.test(s)) return 'series';
    if (/(^|[^a-z])(movies?|peliculas?|films?)([^a-z]|$)/.test(s)) return 'movie';
    return 'auto';
  };

  const handleFinish = () => {
    const pinRaw = String(managerPin || '').replace(/\\D/g, '');
    const pin = pinRaw ? pinRaw.slice(0, 6) : undefined;
    if (!pin || pin.length < 4) {
      window.alert(t('manager_pin_required_alert'));
      return;
    }

    const managerProfile: UserProfile = {
      id: 'manage-1',
      name: managerName,
      avatarColor: AVATAR_COLORS[0],
      isKid: false,
      isManager: true,
      pin,
      language: managerLanguage
    };
      // Persistir configuración al backend
      (async () => {
         try {
             const media_root_types: Record<string, string> = {};
             for (const p of paths) {
               const ty = pathTypes[p] ?? 'auto';
               if (ty !== 'auto') media_root_types[p] = ty;
             }
             const cfg = {
                setupComplete: true,
               // Ajustes: idioma UI/proveedor y localización
               target_lang: uiLangFromLocale(managerLanguage),
               ui_lang_source: (langTouched || getStoredUiLangSource() === 'manual' || getStoredUiLang()) ? 'manual' : 'system',
               tmdb_localization: tmdbLocalization,
               admin_pin: pin,
               profiles: [managerProfile],
                 metadata: metadata,
                 media_roots: paths,
                 media_root_types,
                 loose_movie_roots: [],
              };
             await MediaService.saveAppConfig(cfg);
            // Guardar credenciales TMDB (si hay) y validar
            if (tmdbApiKey && tmdbApiKey.trim()) {
               try {
                  await MediaService.adminLogin(pin);
                  const resp = await MediaService.saveCredentials({ tmdb_api_key: tmdbApiKey.trim() });
                  // If backend returned validation result and it's false, ask user whether to continue
                  if (resp && resp.tmdb_ok === false) {
                     setTmdbStatusMsg(`${t('tmdb_validation_failed')}: ${resp.detail || t('unknown')}`);
                     const proceed = window.confirm(`${t('tmdb_validation_failed')}: ${resp.detail || t('unknown')}. ${t('continue_anyway')}`);
                     if (!proceed) {
                        return; // abort finish so user can fix key
                     }
                  } else if (resp && resp.tmdb_ok) {
                     const started = resp.enrichment_started ? t('enrich_started') : t('enrich_already_running');
                     setTmdbStatusMsg(`${t('tmdb_key_ok')}. ${started}`);
                  }
               } catch (e) {
                  setTmdbStatusMsg(`${t('tmdb_save_failed_confirm')}: ${String(e)}`);
                  // ask user whether to continue if save failed
                  const proceed = window.confirm(`${t('tmdb_save_failed_confirm')}: ${String(e)}. ${t('continue_anyway')}`);
                  if (!proceed) return;
               }
             }
             // Backend guarda roots; notificar al padre
             onComplete(managerProfile, paths, metadata);
         } catch (e) {
             // Fallback a memoria si falla
             onComplete(managerProfile, paths, metadata);
         }
      })();
  };

  const addPath = () => {
    const p = String(pathInput || '').trim();
    if (p && !paths.includes(p)) {
      setPaths([...paths, p]);
      setPathTypes((prev) => (prev[p] ? prev : { ...prev, [p]: inferRootTypeForPath(p) }));
      setPathInput('');
    }
  };

  const handlePickRoots = () => setPickerOpen(true);

  const removePath = (path: string) => {
    setPaths(paths.filter(p => p !== path));
    setPathTypes((prev) => {
      const next = { ...prev };
      delete next[path];
      return next;
    });
  };

  return (
    <div className="min-h-dvh bg-[#0b1220] flex items-center justify-center p-6 pt-[calc(1.5rem+env(safe-area-inset-top,0px))] relative overflow-y-auto">
      {/* Background Ambience */}
      <div className="absolute inset-0 bg-gradient-to-br from-indigo-900/20 via-[#0b1220] to-[#0b1220] z-0" />
      <div className="absolute -top-40 -right-40 w-96 h-96 bg-purple-600/20 rounded-full blur-3xl animate-pulse" />
      <div className="absolute -bottom-40 -left-40 w-96 h-96 bg-indigo-600/20 rounded-full blur-3xl animate-pulse" />

      <div className="relative z-10 w-full max-w-4xl bg-[#1e293b]/60 backdrop-blur-xl border border-white/10 rounded-2xl shadow-2xl flex flex-col overflow-hidden min-h-[600px] animate-fade-in">
        
        {/* Progress Bar */}
        <div className="h-1 bg-slate-800 w-full">
           <div 
             className="h-full bg-gradient-to-r from-indigo-500 to-purple-500 transition-all duration-500"
             style={{ width: `${(step / 3) * 100}%` }}
           />
        </div>

        <div className="flex-1 flex">
          {/* Sidebar Steps */}
          <div className="w-64 bg-slate-900/50 p-8 border-r border-white/5 hidden md:block">
            <h2 className="text-xl font-bold text-white mb-8 tracking-tight">{t('setup_title')}</h2>
            <div className="space-y-6">
              {[
                { num: 1, label: t('setup_step_manager'), icon: User },
                { num: 2, label: t('setup_step_libraries'), icon: FolderPlus },
                { num: 3, label: t('setup_step_metadata'), icon: Database },
              ].map((item) => (
                <div key={item.num} className={`flex items-center space-x-3 ${step === item.num ? 'text-indigo-400' : step > item.num ? 'text-green-400' : 'text-slate-500'}`}>
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold border ${
                    step === item.num ? 'border-indigo-400 bg-indigo-500/10' : 
                    step > item.num ? 'border-green-400 bg-green-500/10' : 
                    'border-slate-600'
                  }`}>
                    {step > item.num ? <Check size={14} /> : item.num}
                  </div>
                  <span className="font-medium">{item.label}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Main Content */}
          <div className="flex-1 p-8 md:p-12 flex flex-col">
            
            {step === 1 && (
              <div className="flex-1 animate-slide-up">
                <div className="mb-8">
                  <div className="w-12 h-12 bg-indigo-500/20 rounded-xl flex items-center justify-center text-indigo-400 mb-4">
                     <User size={24} />
                  </div>
                  <h1 className="text-3xl font-bold text-white mb-2">{t('setup_manager_title')}</h1>
                  <p className="text-slate-400">{t('setup_manager_subtitle')}</p>
                </div>

                <div className="space-y-6 max-w-md">
                   <div>
                      <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">{t('display_name')}</label>
                      <input 
                         type="text" 
                         value={managerName}
                         onChange={(e) => setManagerName(e.target.value)}
                         className="w-full bg-slate-800 border border-white/10 rounded-lg px-4 py-3 text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent outline-none transition-all placeholder-slate-600"
                         placeholder={t('display_name_placeholder')}
                         autoFocus
                      />
                   </div>
                   
                    <div>
                       <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">{t('ui_language')}</label>
                       <select 
                         value={managerLanguage}
                         onChange={(e) => {
                           setLangTouched(true);
                           setManagerLanguage(e.target.value);
                         }}
                         className="w-full bg-slate-800 border border-white/10 rounded-lg px-4 py-3 text-white focus:ring-2 focus:ring-indigo-500 outline-none"
                       >
                          <option value="es-419">{t('spanish')}</option>
                          <option value="en-US">{t('english_us')}</option>
                       </select>
                    </div>

                   <div>
                      <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">{t('pin_required')}</label>
                      <input
                        type="password"
                        inputMode="numeric"
                        value={managerPin}
                        onChange={(e) => setManagerPin(String(e.target.value || '').replace(/\\D/g, '').slice(0, 6))}
                        className="w-full bg-slate-800 border border-white/10 rounded-lg px-4 py-3 text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent outline-none transition-all placeholder-slate-600"
                        placeholder={t('pin_digits_placeholder')}
                      />
                      <p className="text-xs text-slate-500 mt-2">{t('pin_required_desc')}</p>
                   </div>
                 </div>
               </div>
             )}

            {step === 2 && (
              <div className="flex-1 animate-slide-up">
                <div className="mb-8">
                   <div className="w-12 h-12 bg-indigo-500/20 rounded-xl flex items-center justify-center text-indigo-400 mb-4">
                     <FolderPlus size={24} />
                  </div>
                  <h1 className="text-3xl font-bold text-white mb-2">{t('setup_libraries_title')}</h1>
                  <p className="text-slate-400">{t('setup_libraries_subtitle')}</p>
                </div>

                <div className="space-y-6 max-w-xl">
                  <div className="flex gap-3">
                              <input
                      type="text"
                      value={pathInput}
                      onChange={(e) => setPathInput(e.target.value)}
                      placeholder={t('library_path_placeholder')}
                      className="flex-1 bg-slate-800 border border-white/10 rounded-lg px-4 py-3 text-white focus:ring-2 focus:ring-indigo-500 outline-none placeholder-slate-600 font-mono text-sm"
                      onKeyDown={(e) => e.key === 'Enter' && addPath()}
                    />
                    <Button onClick={addPath} icon={<FolderPlus size={18}/>}>{t('add')}</Button>
                  </div>

                               <div className="flex items-center gap-2">
                                 <Button onClick={handlePickRoots} icon={<FolderPlus size={18}/>}>{t('select_folder')}</Button>
                               </div>

                  <div className="bg-slate-900/50 rounded-xl border border-white/5 min-h-[200px] p-2">
                     {paths.length === 0 ? (
                        <div className="h-full flex flex-col items-center justify-center text-slate-500 py-10">
                           <HardDrive size={32} className="mb-3 opacity-50"/>
                           <p>{t('no_paths_yet')}</p>
                        </div>
                     ) : (
                        <div className="space-y-2">
                           {paths.map((path) => (
                               <div key={path} className="flex items-center justify-between p-3 bg-slate-800/50 rounded-lg group">
                                 <div className="flex items-center space-x-3 min-w-0">
                                    <FolderPlus size={16} className="text-indigo-400" />
                                    <span className="font-mono text-sm text-slate-300 truncate">{path}</span>
                                 </div>
                                 <div className="flex items-center gap-2">
                                   <label className="sr-only">{t('root_type_label')}</label>
                                   <select
                                     value={pathTypes[path] ?? 'auto'}
                                     onChange={(e) => {
                                       const v = String(e.target.value || 'auto') as RootTypeChoice;
                                       setPathTypes((prev) => ({ ...prev, [path]: v }));
                                     }}
                                     className="bg-slate-900/50 border border-white/10 rounded-lg px-2 py-1 text-xs text-white focus:ring-2 focus:ring-indigo-500 outline-none"
                                     title={t('root_type_label')}
                                   >
                                     <option value="auto">{t('root_type_auto')}</option>
                                     <option value="movie">{t('root_type_movies')}</option>
                                     <option value="series">{t('root_type_series')}</option>
                                     <option value="anime">{t('root_type_anime')}</option>
                                   </select>
                                   <button onClick={() => removePath(path)} className="text-slate-500 hover:text-red-400 transition-colors">
                                      <Trash2 size={16} />
                                   </button>
                                 </div>
                              </div>
                           ))}
                        </div>
                     )}
                  </div>
                </div>
              </div>
            )}

            {step === 3 && (
               <div className="flex-1 animate-slide-up">
                  <div className="mb-8">
                     <div className="w-12 h-12 bg-indigo-500/20 rounded-xl flex items-center justify-center text-indigo-400 mb-4">
                        <Database size={24} />
                     </div>
                     <h1 className="text-3xl font-bold text-white mb-2">{t('setup_metadata_title')}</h1>
                     <p className="text-slate-400">{t('setup_metadata_subtitle')}</p>
                  </div>

                  <div className="space-y-8 max-w-2xl">
                     <div>
                        <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">{t('movies_tv_agent')}</label>
                        <div className="grid grid-cols-3 gap-4">
                           {['tmdb', 'tvdb', 'omdb'].map((provider) => (
                              <div 
                                 key={provider}
                                 onClick={() => setMetadata({...metadata, moviesProvider: provider as any})}
                                 className={`cursor-pointer p-4 rounded-xl border-2 transition-all ${metadata.moviesProvider === provider ? 'border-indigo-500 bg-indigo-500/10' : 'border-slate-700 bg-slate-800/30 hover:border-slate-500'}`}
                              >
                                 <div className="font-bold text-white uppercase mb-1">{provider}</div>
                                 <div className="text-xs text-slate-400">{t('metadata_provider_desc')}</div>
                              </div>
                           ))}
                        </div>
                     </div>

                        <div>
                           <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">{t('tmdb_api_key_optional')}</label>
                                        <div className="flex gap-2">
                                             <input
                                                 type={showTmdbKey ? 'text' : 'password'}
                                                 value={tmdbApiKey}
                                                 onChange={(e) => setTmdbApiKey(e.target.value)}
                                                 placeholder={t('tmdb_api_key_placeholder_short')}
                                                 className="flex-1 bg-slate-800 border border-white/10 rounded-lg px-4 py-3 text-white focus:ring-2 focus:ring-indigo-500 outline-none placeholder-slate-600"
                                             />
                                             <Button size="sm" onClick={async () => {
                                                if (!tmdbApiKey || !tmdbApiKey.trim()) return;
                                                setTmdbChecking(true);
                                                setTmdbStatusMsg(t('checking'));
                                                try {
                                                   const r = await MediaService.checkCredentials({ tmdb_api_key: tmdbApiKey.trim() });
                                                   if (r && r.ok) {
                                                      setTmdbStatusMsg(t('tmdb_ok'));
                                                   } else {
                                                      setTmdbStatusMsg(`${t('tmdb_error')}: ${r?.detail || t('failed')}`);
                                                      const proceed = window.confirm(`${t('tmdb_validation_failed')}: ${r?.detail || t('unknown')}. ${t('continue_anyway')}`);
                                                      if (!proceed) return;
                                                   }
                                                } catch (e) {
                                                   setTmdbStatusMsg(String(e));
                                                } finally {
                                                   setTmdbChecking(false);
                                                }
                                             }}>{tmdbChecking ? t('checking') : t('validate')}</Button>
                                        </div>
                           <label className="mt-2 inline-flex items-center gap-2 text-xs text-slate-400">
                             <input
                               type="checkbox"
                               checked={showTmdbKey}
                               onChange={(e) => setShowTmdbKey(e.target.checked)}
                               className="rounded border-slate-600 bg-slate-800 text-indigo-500 focus:ring-indigo-500"
                             />
                             {t('show')}
                           </label>
                           <div className="mt-4 flex items-start gap-3 rounded-xl border border-white/10 bg-slate-800/30 p-4">
                             <input
                               type="checkbox"
                               checked={tmdbLocalization}
                               onChange={(e) => setTmdbLocalization(!!e.target.checked)}
                               className="mt-1 h-4 w-4 accent-indigo-500"
                             />
                             <div className="flex-1">
                               <div className="text-sm font-semibold text-white">{t('tmdb_localize_title_short')}</div>
                               <div className="text-xs text-slate-400 mt-1">
                                 {t('tmdb_localize_prefix')} <span className="text-slate-200">{systemUiLang}</span>. {t('tmdb_localize_suffix')}
                               </div>
                             </div>
                           </div>
                           <p className="text-xs text-slate-400 mt-2">{t('tmdb_key_help')}</p>
                           {tmdbStatusMsg && (
                             <p className="text-xs text-emerald-400 mt-2">{tmdbStatusMsg}</p>
                           )}
                        </div>

                      <div>
                         <label className="block text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">{t('anime_agent')}</label>
                         <div className="grid grid-cols-2 gap-4">
                            {[
                              { id: 'jikan', title: 'Jikan', hint: 'MyAnimeList (recomendado)' },
                              { id: 'kitsu', title: 'Kitsu', hint: 'Alternativa (mas estable)' },
                            ].map((p) => (
                               <div
                                  key={p.id}
                                  onClick={() => setMetadata({ ...metadata, animeProvider: p.id as any })}
                                  className={`cursor-pointer p-4 rounded-xl border-2 transition-all ${
                                    metadata.animeProvider === p.id
                                      ? 'border-purple-500 bg-purple-500/10'
                                      : 'border-slate-700 bg-slate-800/30 hover:border-slate-500'
                                  }`}
                               >
                                  <div className="font-bold text-white mb-1">{p.title}</div>
                                  <div className="text-xs text-slate-400">{p.hint}</div>
                               </div>
                            ))}
                         </div>
                      </div>
                  </div>
               </div>
            )}

            <FolderPickerModal
              open={pickerOpen}
              onClose={() => setPickerOpen(false)}
              title={t('select_library')}
              onConfirm={(picked) => {
                const list = Array.isArray(picked) ? picked : [];
                if (!list.length) {
                  setPickerOpen(false);
                  return;
                }
                setPaths((prev) => {
                  const next = [...prev];
                  for (const p of list) {
                    const path = String(p || '').trim();
                    if (path && !next.includes(path)) next.push(path);
                  }
                  return next;
                });
                setPathTypes((prev) => {
                  const next = { ...prev };
                  for (const p of list) {
                    const path = String(p || '').trim();
                    if (path && !next[path]) next[path] = inferRootTypeForPath(path);
                  }
                  return next;
                });
                setPickerOpen(false);
              }}
            />

            {/* Footer Actions */}
            <div className="mt-12 flex justify-between items-center border-t border-white/5 pt-6">
               {step > 1 ? (
                  <button onClick={() => setStep(s => s - 1)} className="text-slate-400 hover:text-white transition-colors">
                     {t('back')}
                  </button>
               ) : ( <div></div> )}

               {step < 3 ? (
                  <Button 
                    onClick={() => void handleNext()} 
                     disabled={step === 1 ? (!managerName || String(managerPin || '').replace(/\\D/g, '').length < 4) : step === 2 ? paths.length === 0 : false}
                     icon={<ArrowRight size={18} />}
                     className="flex-row-reverse space-x-reverse" // Hack to put icon on right if Button supports it or just use children
                  >
                     {t('next_step')}
                  </Button>
               ) : (
                  <Button onClick={handleFinish} size="lg" className="px-8 font-bold shadow-indigo-500/50">
                     {t('finish_and_scan')}
                  </Button>
               )}
            </div>

          </div>
        </div>
      </div>
    </div>
  );
};
