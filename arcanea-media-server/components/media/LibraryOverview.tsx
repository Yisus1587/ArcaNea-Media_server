import React, { useEffect, useState } from 'react';
import { MediaService, resolveAssetUrl } from '../../services/api';
import { MediaCard } from './MediaCard';
import { MediaItem } from '../../types';
import { useI18n } from '../../i18n/i18n';
import { Button } from '../ui/Button';

type LibraryGroup = { key: any; label: string; count: number; sample: any[] };

export const LibraryOverview: React.FC<{ onSelectMedia?: (it: MediaItem) => void; groupBy?: string; sample?: number }> = ({ onSelectMedia, groupBy = 'library_type', sample = 6 }) => {
  const { t } = useI18n();
  const [groups, setGroups] = useState<LibraryGroup[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const data = await MediaService.getLibraryOverview(groupBy, sample);
        if (!mounted) return;
        setGroups(Array.isArray(data?.groups) ? data.groups : []);
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn('Failed to load library overview', e);
      } finally {
        if (mounted) setLoading(false);
      }
    })();
    return () => {
      mounted = false;
    };
  }, [groupBy, sample]);

  function _mapSample(s: any): MediaItem {
    return {
      id: String(s?.id ?? ''),
      title: String(s?.title ?? ''),
      path: String(s?.path ?? ''),
      type: ((): any => {
        const m = String(s?.media_type || '').toLowerCase();
        if (m.includes('anime')) return 'anime';
        if (m.includes('tv') || m.includes('series') || m.includes('show')) return 'series';
        return 'movie';
      })(),
      addedAt: s?.created_at || new Date().toISOString(),
      thumbnailUrl: resolveAssetUrl(s?.poster_url || `/api/media/${s?.id}/poster`),
      posterPath: resolveAssetUrl(s?.poster_url || `/api/media/${s?.id}/poster`),
    } as MediaItem;
  }

  if (loading) {
    return <div className="py-10 text-center text-slate-300">{t('loading')}</div>;
  }

  if (!groups.length) {
    return <div className="py-10 text-center text-slate-300">{t('no_content')}</div>;
  }

  return (
    <div className="space-y-8">
      {groups.map((g) => (
        <section key={String(g.key)} className="space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-lg font-semibold text-white">
              {g.label}
              <span className="text-sm text-slate-400 ml-2">({g.count})</span>
            </h3>
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" onClick={() => {}}>
                {t('view_all')}
              </Button>
            </div>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3 sm:gap-6">
            {(g.sample || []).map((s) => (
              <MediaCard key={String(s?.id)} item={_mapSample(s)} onClick={(it) => onSelectMedia?.(it)} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
};

export default LibraryOverview;
