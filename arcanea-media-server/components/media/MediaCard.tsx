import React from 'react';
import { Play, Star, ImageOff } from 'lucide-react';
import { MediaItem } from '../../types';
import { useI18n } from '../../i18n/i18n';

interface MediaCardProps {
  item: MediaItem;
  onClick: (item: MediaItem) => void;
}

const MediaCardImpl: React.FC<MediaCardProps> = ({ item, onClick }) => {
  const { t } = useI18n();
  const fallbackPoster = '/images/arcanea-poster.svg';
  const hasImage = !!((item as any).posterPath || item.thumbnailUrl || item.backdropUrl);
  const imageSrc = (item as any).posterPath || item.thumbnailUrl || item.backdropUrl || fallbackPoster;
  const isLocal = (item as any).is_identified === false;
  
  // Simulated progress for visual demonstration if needed, 
  // but let's look for actual data if available
  const progress = (item as any).watchProgress || 0; 

  return (
    <div 
      className="media-card group relative flex flex-col cursor-pointer animate-fade-in transition-all duration-500"
      onClick={() => onClick(item)}
    >
      {/* Image Container with Aspect Ratio 2:3 */}
      <div className="relative w-full aspect-[2/3] rounded-[24px] overflow-hidden shadow-[0_15px_35px_rgba(0,0,0,0.5)] bg-[#0f172a] border border-white/5 transition-all duration-700 group-hover:shadow-[0_25px_60px_rgba(79,70,229,0.4)] group-hover:-translate-y-2 group-hover:border-indigo-500/50">
        <img
          src={imageSrc}
          alt={item.title}
          loading="lazy"
          decoding="async"
          className="w-full h-full object-cover transition-transform duration-[1200ms] ease-out group-hover:scale-110"
        />
        
        {!hasImage && (
          <div className="absolute inset-0 flex items-center justify-center text-slate-200/70 bg-slate-900">
            <div className="flex flex-col items-center gap-3">
              <div className="p-5 rounded-3xl bg-white/5 border border-white/10 backdrop-blur-xl">
                <ImageOff size={28} className="text-slate-500" />
              </div>
              <span className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-500">{t('no_image')}</span>
            </div>
          </div>
        )}
        
        {/* Layered Gradients */}
        <div className="absolute inset-0 bg-gradient-to-t from-[#05070c] via-transparent to-transparent opacity-60 group-hover:opacity-90 transition-opacity duration-500" />
        <div className="absolute inset-0 bg-gradient-to-b from-indigo-500/10 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-700" />
        
        {/* Shine Overlay */}
        <div className="absolute inset-0 bg-gradient-to-tr from-white/5 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-1000" />

        {/* Hover Play Button */}
        <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-all duration-500 bg-indigo-950/20 backdrop-blur-[2px]">
          <div className="w-16 h-16 rounded-full bg-white text-black flex items-center justify-center shadow-[0_0_40px_rgba(255,255,255,0.4)] transform translate-y-6 group-hover:translate-y-0 transition-all duration-500 ease-out hover:scale-110 active:scale-90">
            <Play fill="currentColor" className="ml-1" size={32} />
          </div>
        </div>

        {/* Top Floating Badges */}
        <div className="absolute top-3 left-3 right-3 flex justify-between items-start pointer-events-none transition-transform duration-500 group-hover:translate-y-[-2px]">
          <div className="flex flex-col items-start gap-1.5">
            {isLocal ? (
              <div className="px-2.5 py-1 rounded-xl bg-amber-500/90 backdrop-blur-xl text-[9px] font-black uppercase tracking-wider text-black shadow-xl border border-amber-400/20">
                {t('local_badge')}
              </div>
            ) : (
              <div className="px-2.5 py-1 rounded-xl bg-indigo-600/90 backdrop-blur-xl text-[9px] font-black uppercase tracking-wider text-white shadow-xl border border-indigo-500/20">
                {item.type === 'movie' ? 'FILM' : 'SERIES'}
              </div>
            )}
            {item.isAiring ? (
              <div className="px-2.5 py-1 rounded-xl bg-emerald-500/90 backdrop-blur-xl text-[9px] font-black uppercase tracking-wider text-black shadow-xl border border-emerald-400/20">
                {t('airing_badge')}
              </div>
            ) : null}
          </div>

          {item.rating && (
            <div className="px-2.5 py-1 rounded-xl bg-black/40 backdrop-blur-xl border border-white/10 flex items-center gap-1.5 shadow-xl">
              <Star size={12} className="text-yellow-400 fill-yellow-400" />
              <span className="text-[10px] font-black text-white">{item.rating.toFixed(1)}</span>
            </div>
          )}
        </div>

        {/* Dynamic Progress Bar */}
        {progress > 0 && (
          <div className="absolute bottom-0 left-0 right-0 h-2 bg-black/40 backdrop-blur-md overflow-hidden">
            <div 
              className="h-full bg-gradient-to-r from-indigo-600 via-indigo-400 to-indigo-600 bg-[length:200%_100%] animate-shimmer shadow-[0_0_15px_rgba(79,70,229,0.8)]"
              style={{ width: `${progress}%` }}
            />
          </div>
        )}
      </div>

      {/* Info Section */}
      <div className="mt-4 px-2 space-y-1">
        <h3 className="text-[0.95rem] font-black text-white leading-tight line-clamp-1 group-hover:text-indigo-400 transition-colors duration-300">
          {item.title}
        </h3>
        <div className="flex items-center gap-2.5 opacity-50 group-hover:opacity-100 transition-opacity duration-500">
           <span className="text-[11px] font-bold text-slate-300">
            {item.year}
          </span>
          <span className="w-1 h-1 rounded-full bg-slate-700" />
          <span className="text-[9px] font-black uppercase tracking-widest text-slate-500">
            {(item as any).quality || 'HD'}
          </span>
        </div>
      </div>
    </div>
  );
};

export const MediaCard = React.memo(MediaCardImpl);
