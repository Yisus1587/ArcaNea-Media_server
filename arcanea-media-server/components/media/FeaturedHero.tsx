import React from 'react';
import { Play, Info } from 'lucide-react';
import { Button } from '../ui/Button';
import { MediaItem } from '../../types';
import { useI18n } from '../../i18n/i18n';

interface FeaturedHeroProps {
  item: MediaItem;
  onPlay: (item: MediaItem) => void;
  onInfo: (item: MediaItem) => void;
  eyebrow?: string;
  insight?: string | null;
  totalVariants?: number;
  activeVariant?: number;
}

export const FeaturedHero: React.FC<FeaturedHeroProps> = ({ item, onPlay, onInfo, eyebrow, insight, totalVariants = 1, activeVariant = 0 }) => {
  const { t } = useI18n();
  return (
    <div className="relative h-[400px] sm:h-[450px] lg:h-[600px] rounded-[32px] overflow-hidden shadow-[0_40px_100px_rgba(0,0,0,0.7)] group cursor-pointer mb-8 sm:mb-12 mt-4 sm:mt-0 border border-white/10 bg-[#0b1220] transition-all duration-700 hover:shadow-indigo-500/20" onClick={() => onInfo(item)}>
      {/* Background Image with Parallax-like effect on hover */}
      <div className="absolute inset-0 transition-transform duration-[3000ms] ease-out group-hover:scale-110">
        <img 
          src={item.backdropUrl || item.thumbnailUrl} 
          alt={`${t('featured')}: ${item.title}`} 
          className="w-full h-full object-cover opacity-70 sm:opacity-80 transition-opacity duration-700 group-hover:opacity-90"
        />
      </div>

      {/* Advanced Layered Gradients */}
      <div className="absolute inset-0 bg-gradient-to-t from-[#05070c] via-[#05070c]/60 to-transparent" />
      <div className="absolute inset-0 bg-gradient-to-r from-[#05070c] via-[#05070c]/30 to-transparent hidden sm:block" />
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_20%_30%,rgba(99,102,241,0.2),transparent_60%)]" />
      
      {/* Content Container */}
      <div className="absolute inset-0 flex flex-col justify-end p-6 sm:p-12 lg:p-20 w-full z-10">
        <div className="max-w-4xl space-y-4 sm:space-y-6">
          <div className="flex flex-wrap items-center gap-2 sm:gap-3 animate-in fade-in slide-in-from-left-4 duration-500">
            <div className="px-3 py-1 rounded-full bg-indigo-600 text-white text-[10px] sm:text-xs font-black uppercase tracking-widest shadow-lg shadow-indigo-600/40">
              {eyebrow || t('featured')}
            </div>
            {item.year ? (
              <div className="px-3 py-1 rounded-full bg-white/10 backdrop-blur-xl border border-white/20 text-slate-100 text-[10px] sm:text-xs font-bold">
                {item.year}
              </div>
            ) : null}
            {item.genre && item.genre.length > 0 && (
              <div className="px-3 py-1 rounded-full bg-white/5 backdrop-blur-xl border border-white/10 text-slate-300 text-[10px] sm:text-xs font-medium hidden sm:block">
                {item.genre[0]}
              </div>
            )}
            {item.rating && (
              <div className="px-3 py-1 rounded-full bg-emerald-500/20 backdrop-blur-xl border border-emerald-500/30 text-emerald-400 text-[10px] sm:text-xs font-black">
                {Math.round(item.rating * 10)}% Match
              </div>
            )}
          </div>

          <h1 className="text-4xl sm:text-6xl lg:text-8xl font-black leading-[1] tracking-tight text-white drop-shadow-[0_15px_45px_rgba(0,0,0,0.6)] animate-in fade-in slide-in-from-left-6 duration-700 delay-75 group-hover:translate-x-1 transition-transform">
            {item.title}
          </h1>

          {insight ? (
            <div className="inline-flex items-center rounded-full border border-cyan-400/20 bg-cyan-400/10 px-3 py-1 text-[11px] font-semibold tracking-wide text-cyan-100">
              {insight}
            </div>
          ) : null}
          
          <p className="text-slate-200/90 text-sm sm:text-lg lg:text-xl line-clamp-2 sm:line-clamp-3 max-w-2xl font-medium leading-relaxed drop-shadow-lg animate-in fade-in slide-in-from-left-8 duration-1000 delay-150">
            {item.overview || item.synopsis || t('no_overview')}
          </p>

          <div className="flex flex-wrap gap-4 pt-2 animate-in fade-in slide-in-from-bottom-4 duration-1000 delay-300">
              <Button 
                  onClick={(e) => { e.stopPropagation(); onPlay(item); }} 
                  icon={<Play fill="currentColor" size={24} />}
                  className="h-14 sm:h-16 px-8 sm:px-12 bg-white text-black font-black text-base sm:text-xl rounded-[20px] shadow-[0_20px_50px_rgba(255,255,255,0.2)] hover:bg-indigo-50 hover:scale-105 active:scale-95 transition-all duration-300 flex-1 sm:flex-none justify-center"
              >
                  {t('play')}
              </Button>
              <Button 
                  variant="ghost" 
                  onClick={(e) => { e.stopPropagation(); onInfo(item); }}
                  icon={<Info size={24} />}
                  className="h-14 sm:h-16 px-8 sm:px-12 bg-white/10 backdrop-blur-2xl border border-white/20 text-white font-black text-base sm:text-xl rounded-[20px] hover:bg-white/20 hover:scale-105 active:scale-95 transition-all duration-300 flex-1 sm:flex-none justify-center"
              >
                  {t('info')}
              </Button>
          </div>
        </div>
      </div>

      {/* Interactive Glow at bottom */}
      <div className="absolute bottom-0 left-0 right-0 h-48 bg-gradient-to-t from-indigo-500/20 via-indigo-500/5 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-1000 pointer-events-none" />
      {totalVariants > 1 ? (
        <div className="absolute right-6 top-6 z-20 flex items-center gap-2 rounded-full border border-white/10 bg-black/30 px-3 py-2 backdrop-blur-xl">
          {Array.from({ length: totalVariants }).map((_, idx) => (
            <span
              key={idx}
              className={`h-1.5 rounded-full transition-all duration-500 ${idx === activeVariant ? 'w-6 bg-cyan-300' : 'w-1.5 bg-white/35'}`}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
};
