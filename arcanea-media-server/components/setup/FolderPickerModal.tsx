import React, { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { X, ArrowLeft, Folder, HardDrive, Check } from 'lucide-react';
import { Button } from '../ui/Button';
import { MediaService } from '../../services/api';
import { useI18n } from '../../i18n/i18n';

type FsEntry = { name: string; path: string; type: string };

export function FolderPickerModal(props: {
  open: boolean;
  onClose: () => void;
  onPick: (path: string) => void;
  title?: string;
}) {
  const { open, onClose, onPick, title } = props;
  const { t } = useI18n();
  const [curPath, setCurPath] = useState<string | null>(null);
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<FsEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canPick = useMemo(() => typeof curPath === 'string' && curPath.trim().length > 0, [curPath]);

  useEffect(() => {
    if (!open) return;
    setCurPath(null);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await MediaService.listDirectories(curPath);
        if (cancelled) return;
        setParent(res.parent ?? null);
        setEntries(Array.isArray(res.entries) ? res.entries : []);
      } catch (e: any) {
        if (cancelled) return;
        setEntries([]);
        setParent(null);
        setError(String(e?.message || e || 'Error'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, curPath]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

  if (!open) return null;
  if (typeof document === 'undefined') return null;

  const modal = (
    <div translate="no" className="notranslate fixed inset-0 z-[240] bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="w-full max-w-2xl rounded-2xl bg-slate-900/90 border border-white/10 shadow-2xl overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b border-white/10">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-9 h-9 rounded-xl bg-indigo-500/15 flex items-center justify-center text-indigo-300">
              <Folder size={18} />
            </div>
            <div className="min-w-0">
              <div className="text-sm font-semibold text-white truncate">{title || t('select_folder_title')}</div>
              <div className="text-xs text-slate-400 truncate">{curPath || t('drives_root')}</div>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-white/5 text-slate-300 hover:text-white transition"
            aria-label={t('close')}
          >
            <X size={18} />
          </button>
        </div>

        <div className="px-5 py-4">
          <div className="flex items-center gap-2 mb-3">
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => setCurPath(parent)}
              disabled={!parent || loading}
              icon={<ArrowLeft size={16} />}
            >
              {t('go_up')}
            </Button>
            <div className="flex-1" />
            <Button
              type="button"
              size="sm"
              onClick={() => {
                if (!canPick) return;
                onPick(String(curPath));
              }}
              disabled={!canPick || loading}
              icon={<Check size={16} />}
            >
              {t('use_this_folder')}
            </Button>
          </div>

          <div className="rounded-xl border border-white/10 bg-slate-950/40 overflow-hidden">
            <div className="max-h-[420px] overflow-auto">
              {loading ? (
                <div className="p-6 text-sm text-slate-400">{t('loading')}</div>
              ) : error ? (
                <div className="p-6 text-sm text-red-300">{t('error_label')}: {error}</div>
              ) : entries.length === 0 ? (
                <div className="p-6 text-sm text-slate-500">{t('no_subfolders')}</div>
              ) : (
                entries.map((e) => (
                  <button
                    key={e.path}
                    type="button"
                    onClick={() => setCurPath(e.path)}
                    className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-white/5 transition border-b border-white/5 last:border-b-0"
                  >
                    <div className="w-8 h-8 rounded-lg bg-white/5 flex items-center justify-center text-slate-300">
                      {String(e.type).toLowerCase() === 'drive' ? <HardDrive size={16} /> : <Folder size={16} />}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm text-slate-100 truncate">{e.name}</div>
                      <div className="text-[11px] text-slate-500 truncate">{e.path}</div>
                    </div>
                  </button>
                ))
              )}
            </div>
          </div>

          <div className="mt-3 text-[11px] text-slate-500">
            {t('tip_paste_path')}
          </div>
        </div>
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}
