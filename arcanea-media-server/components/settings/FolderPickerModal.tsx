import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X, Folder, HardDrive, Cloud, ChevronLeft, CheckSquare, Square, Loader2, RefreshCw } from 'lucide-react';
import { Button } from '../ui/Button';
import { MediaService } from '../../services/api';
import { useI18n } from '../../i18n/i18n';

type Entry = { name: string; path: string; type: string };
type Source = 'local' | 'drive';

function _labelFor(entry: Entry): string {
  return entry.name || entry.path || '-';
}

function _normalizeEntries(raw: unknown): Entry[] {
  if (!Array.isArray(raw)) return [];
  const out: Entry[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    const rec = (item || {}) as Record<string, unknown>;
    const path = String(rec.path || '').trim();
    if (!path) continue;
    const dedupeKey = path.toLowerCase();
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    out.push({
      name: String(rec.name || path),
      path,
      type: String(rec.type || 'folder'),
    });
  }
  return out;
}

export function FolderPickerModal(props: {
  open: boolean;
  onClose: () => void;
  onConfirm: (paths: string[]) => void;
  title?: string;
}) {
  const { open, onClose, onConfirm, title } = props;
  const { t } = useI18n();
  const [source, setSource] = useState<Source>('local');
  const [currentPath, setCurrentPath] = useState<string | null>(null);
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [driveReady, setDriveReady] = useState(false);
  const [driveStack, setDriveStack] = useState<Array<{ id: string; name: string }>>([{ id: 'root', name: 'Google Drive' }]);
  const mountedRef = useRef(false);
  const openRef = useRef(open);
  const requestSeqRef = useRef(0);

  const selectedPaths = useMemo(() => Object.keys(selected).filter((k) => selected[k]), [selected]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestSeqRef.current += 1;
    };
  }, []);

  useEffect(() => {
    openRef.current = open;
    if (!open) requestSeqRef.current += 1;
  }, [open]);

  const load = useCallback(async (path: string | null) => {
    const reqId = ++requestSeqRef.current;
    setLoading(true);
    setError(null);
    try {
      const res = await MediaService.listDirectories(path);
      if (!mountedRef.current || !openRef.current || reqId !== requestSeqRef.current) return;
      setCurrentPath(res.path);
      setParent(res.parent);
      setEntries(_normalizeEntries(res.entries));
    } catch (e) {
      if (!mountedRef.current || !openRef.current || reqId !== requestSeqRef.current) return;
      setError(String(e));
      setEntries([]);
    } finally {
      if (mountedRef.current && openRef.current && reqId === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, []);

  const loadDrive = useCallback(async (parentId: string) => {
    const reqId = ++requestSeqRef.current;
    setLoading(true);
    setError(null);
    try {
      const res = await MediaService.listGDriveFolders(parentId);
      if (!mountedRef.current || !openRef.current || reqId !== requestSeqRef.current) return;
      const folders = Array.isArray(res?.folders) ? res.folders : [];
      setEntries(
        folders
          .filter((f: any) => f && f.id)
          .map((f: any) => ({ name: String(f.name || f.id), path: `gdrive://${f.id}`, type: 'drive' })),
      );
    } catch (e) {
      if (!mountedRef.current || !openRef.current || reqId !== requestSeqRef.current) return;
      setError(String(e));
      setEntries([]);
    } finally {
      if (mountedRef.current && openRef.current && reqId === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setSource('local');
    setSelected({});
    setDriveStack([{ id: 'root', name: 'Google Drive' }]);
    void load(null);
    let cancelled = false;
    void MediaService.getGDriveStatus()
      .then((res) => {
        if (!cancelled && mountedRef.current) setDriveReady(!!res?.authenticated);
      })
      .catch(() => {
        if (!cancelled && mountedRef.current) setDriveReady(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, load]);

  const switchSource = (next: Source) => {
    if (next === source) return;
    setSelected({});
    setSource(next);
    if (next === 'drive') {
      setDriveStack([{ id: 'root', name: 'Google Drive' }]);
      void loadDrive('root');
    } else {
      void load(null);
    }
  };

  const goBack = () => {
    if (source === 'drive') {
      if (driveStack.length <= 1) return;
      const next = driveStack.slice(0, -1);
      setDriveStack(next);
      void loadDrive(next[next.length - 1].id);
      return;
    }
    void load(parent);
  };

  const openEntry = (entry: Entry) => {
    if (source === 'drive') {
      const id = entry.path.replace('gdrive://', '');
      setDriveStack((prev) => [...prev, { id, name: entry.name }]);
      void loadDrive(id);
      return;
    }
    void load(entry.path);
  };

  const currentLocation =
    source === 'drive' ? driveStack.map((s) => s.name).join(' / ') : currentPath || t('drives');

  if (!open) return null;
  if (typeof document === 'undefined') return null;

  const modal = (
    <div translate="no" className="notranslate fixed inset-0 z-[240] flex items-center justify-center bg-black/70 backdrop-blur-sm p-4 pt-[calc(1rem+env(safe-area-inset-top,0px))] pb-[calc(1rem+env(safe-area-inset-bottom,0px))]">
      <div className="w-full max-w-3xl rounded-2xl border border-white/10 bg-slate-950/90 shadow-2xl overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b border-white/10">
          <div className="min-w-0">
            <div className="text-white font-semibold">{title || t('select_folders_title')}</div>
            <div className="mt-1 text-xs text-slate-400 font-mono truncate" title={currentLocation}>
              {currentLocation}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-lg text-slate-300 hover:text-white hover:bg-white/10 transition"
            aria-label={t('close')}
          >
            <X size={18} />
          </button>
        </div>

        <div className="px-5 py-3 flex items-center gap-2 flex-wrap">
          <div className="flex items-center rounded-lg border border-white/10 overflow-hidden">
            <button
              type="button"
              onClick={() => switchSource('local')}
              className={`px-3 py-1.5 text-xs flex items-center gap-1.5 transition ${
                source === 'local' ? 'bg-white/10 text-white' : 'text-slate-400 hover:text-white'
              }`}
              title={t('source_this_device')}
            >
              <HardDrive size={13} />
              {t('source_this_device')}
            </button>
            <button
              type="button"
              onClick={() => switchSource('drive')}
              disabled={!driveReady}
              className={`px-3 py-1.5 text-xs flex items-center gap-1.5 border-l border-white/10 transition ${
                source === 'drive' ? 'bg-white/10 text-white' : 'text-slate-400 hover:text-white'
              } ${driveReady ? '' : 'opacity-40 cursor-not-allowed'}`}
              title={driveReady ? 'Google Drive' : t('drive_not_connected')}
            >
              <Cloud size={13} />
              Google Drive
            </button>
          </div>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={goBack}
            disabled={source === 'drive' ? driveStack.length <= 1 || loading : !parent || loading}
            icon={<ChevronLeft size={14} />}
          >
            {t('back')}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => (source === 'drive' ? void loadDrive(driveStack[driveStack.length - 1].id) : void load(currentPath))}
            disabled={loading}
            icon={<RefreshCw size={14} />}
          >
            {t('reload')}
          </Button>
          <div className="ml-auto text-xs text-slate-400">
            {selectedPaths.length} {selectedPaths.length === 1 ? t('selected_single') : t('selected_plural')}
          </div>
        </div>

        <div className="max-h-[55vh] overflow-y-auto px-2 pb-2">
          {loading ? (
            <div className="flex items-center justify-center py-16 text-slate-300">
              <Loader2 className="animate-spin mr-3" size={18} />
              {t('loading')}
            </div>
          ) : error ? (
            <div className="px-4 py-8 text-center text-sm text-red-300">
              {t('list_folder_error')} {error}
            </div>
          ) : entries.length === 0 ? (
            <div className="px-4 py-10 text-center text-sm text-slate-400">
              {source === 'drive' && !driveReady ? t('drive_not_connected') : t('no_subfolders')}
            </div>
          ) : (
            <div className="space-y-1">
              {entries.map((e, idx) => {
                const key = e.path;
                const isSelected = !!selected[key];
                const isRemote = (e.type || '').toLowerCase() === 'drive';
                return (
                  <div
                    key={`${key}::${idx}`}
                    className="flex items-center gap-3 px-3 py-2 rounded-xl hover:bg-white/5 border border-transparent hover:border-white/5 transition"
                  >
                    <button
                      type="button"
                      onClick={() => setSelected((prev) => ({ ...prev, [key]: !prev[key] }))}
                      className="p-1.5 rounded-lg hover:bg-white/5 text-slate-300"
                      title={isSelected ? t('remove_selection') : t('select')}
                      aria-label={isSelected ? t('remove_selection') : t('select')}
                    >
                      {isSelected ? <CheckSquare size={18} /> : <Square size={18} />}
                    </button>

                    <button
                      type="button"
                      onClick={() => openEntry(e)}
                      className="flex-1 min-w-0 flex items-center gap-3 text-left"
                      title={t('open')}
                    >
                      <div className="w-9 h-9 rounded-lg bg-slate-800/60 border border-white/5 flex items-center justify-center text-indigo-200">
                        {isRemote ? <Cloud size={18} /> : <Folder size={18} />}
                      </div>
                      <div className="min-w-0">
                        <div className="text-sm text-white font-medium truncate">{_labelFor(e)}</div>
                        <div className="text-[11px] text-slate-500 font-mono truncate">{e.path}</div>
                      </div>
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="px-5 py-4 border-t border-white/10 flex items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>{t('cancel')}</Button>
          <Button
            type="button"
            onClick={() => onConfirm(selectedPaths)}
            disabled={selectedPaths.length === 0}
            icon={<Folder size={16} />}
          >
            {t('add_selected')}
          </Button>
        </div>
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}
