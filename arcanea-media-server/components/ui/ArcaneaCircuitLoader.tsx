import React from 'react';

type Props = {
  className?: string;
  strokeWidth?: number;
};

export const ArcaneaCircuitLoader: React.FC<Props> = ({ className, strokeWidth = 14 }) => {
  return (
    <div className={className}>
      <style>{`
        @keyframes arcanea-spin { to { transform: rotate(360deg); } }
        @keyframes arcanea-spin-beam { to { transform: rotate(360deg); } }
        @keyframes arcanea-spin-slow { to { transform: rotate(-360deg); } }
        @keyframes arcanea-pulse {
          0%, 100% { opacity: 0.82; filter: drop-shadow(0 0 0 rgba(99, 102, 241, 0)); }
          50% { opacity: 1; filter: drop-shadow(0 0 18px rgba(99, 102, 241, 0.22)); }
        }
        @keyframes arcanea-draw { to { stroke-dashoffset: 0; } }
        @keyframes arcanea-twinkle { 0%,100%{opacity:.62} 50%{opacity:1} }
        @keyframes arcanea-drift {
          0% { transform: translate(-4px,-3px) scale(0.98); }
          50% { transform: translate(0px,0px) scale(1.02); }
          100% { transform: translate(4px,3px) scale(1); }
        }
      `}</style>

      <svg viewBox="0 0 512 512" className="w-full h-auto" role="img" aria-label="ArcaNea loader">
        <defs>
          <linearGradient id="arcanea-grad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#22d3ee" />
            <stop offset="40%" stopColor="#3b82f6" />
            <stop offset="70%" stopColor="#a855f7" />
            <stop offset="100%" stopColor="#f472b6" />
          </linearGradient>
          <linearGradient id="arcanea-core" x1="0" y1="1" x2="1" y2="0">
            <stop offset="0%" stopColor="#0ea5e9" />
            <stop offset="55%" stopColor="#6366f1" />
            <stop offset="100%" stopColor="#f472b6" />
          </linearGradient>
          <radialGradient id="arcanea-glow" cx="50%" cy="45%" r="62%">
            <stop offset="0%" stopColor="rgba(99,102,241,0.22)" />
            <stop offset="55%" stopColor="rgba(168,85,247,0.12)" />
            <stop offset="100%" stopColor="rgba(0,0,0,0)" />
          </radialGradient>
        </defs>

        {/* ambient glow */}
        <circle cx="256" cy="252" r="185" fill="url(#arcanea-glow)" style={{ animation: 'arcanea-pulse 2.6s ease-in-out infinite' }} />

        {/* orbit */}
        <g style={{ transformOrigin: '256px 256px', animation: 'arcanea-spin 2.8s linear infinite', opacity: 0.92 }}>
          <circle cx="256" cy="256" r="176" fill="none" stroke="rgba(255,255,255,0.10)" strokeWidth="2.2" />
          <circle
            cx="256"
            cy="256"
            r="176"
            fill="none"
            stroke="url(#arcanea-grad)"
            strokeWidth="4"
            strokeLinecap="round"
            strokeDasharray="160 740"
          />
        </g>

        {/* counter-orbit */}
        <g style={{ transformOrigin: '256px 256px', animation: 'arcanea-spin-slow 6.2s linear infinite', opacity: 0.6 }}>
          <circle cx="256" cy="256" r="132" fill="none" stroke="rgba(148,163,184,0.18)" strokeWidth="1.6" />
          <circle
            cx="256"
            cy="256"
            r="132"
            fill="none"
            stroke="url(#arcanea-grad)"
            strokeWidth="2.4"
            strokeLinecap="round"
            strokeDasharray="90 480"
          />
        </g>

        {/* scan beam */}
        <g style={{ transformOrigin: '256px 256px', animation: 'arcanea-spin-beam 1.75s linear infinite', opacity: 0.9 }}>
          <path d="M256 256 L256 62 A 194 194 0 0 1 372 106 Z" fill="rgba(99,102,241,0.07)" />
          <path
            d="M256 76 A 180 180 0 0 1 367 110"
            fill="none"
            stroke="rgba(34,211,238,0.35)"
            strokeWidth="3.5"
            strokeLinecap="round"
          />
        </g>

        {/* logo strokes */}
        <g filter="drop-shadow(0 10px 30px rgba(0,0,0,0.18))">
          {/* core badge */}
          <g style={{ animation: 'arcanea-pulse 2.4s ease-in-out infinite' }}>
            <path
              d="M256 148 L326 188 L326 272 L256 312 L186 272 L186 188 Z"
              fill="rgba(11,18,32,0.75)"
              stroke="url(#arcanea-core)"
              strokeWidth="5"
            />
            <path
              d="M256 168 L302 194 L302 266 L256 292 L210 266 L210 194 Z"
              fill="url(#arcanea-core)"
              opacity="0.12"
            />
          </g>
          {[
            { id: 'aOuter', d: 'M 132 410 L 256 108 L 380 410', delay: 0 },
            { id: 'aLeft', d: 'M 170 410 L 256 198', delay: 0.06 },
            { id: 'aRight', d: 'M 342 410 L 256 198', delay: 0.06 },
            { id: 'aBar', d: 'M 210 314 L 302 314', delay: 0.11 },
            { id: 'ltA', d: 'M 160 320 H 98', delay: 0.12 },
            { id: 'ltB', d: 'M 176 284 H 118', delay: 0.14 },
            { id: 'ltC', d: 'M 196 250 H 138', delay: 0.16 },
            { id: 'ltD', d: 'M 214 220 H 156', delay: 0.18 },
            { id: 'rtA', d: 'M 352 320 H 414', delay: 0.12 },
            { id: 'rtB', d: 'M 336 284 H 394', delay: 0.14 },
            { id: 'rtC', d: 'M 316 250 H 374', delay: 0.16 },
            { id: 'rtD', d: 'M 298 220 H 356', delay: 0.18 },
            { id: 'in1', d: 'M 256 198 V 164', delay: 0.12 },
            { id: 'in2', d: 'M 238 314 V 360', delay: 0.16 },
            { id: 'in3', d: 'M 274 314 V 360', delay: 0.16 },
            { id: 'in4', d: 'M 210 314 H 238', delay: 0.17 },
            { id: 'in5', d: 'M 274 314 H 302', delay: 0.17 },
          ].map((p) => (
            <path
              key={p.id}
              d={p.d}
              fill="none"
              stroke="url(#arcanea-grad)"
              strokeWidth={strokeWidth}
              strokeLinecap="round"
              strokeLinejoin="round"
              style={{
                strokeDasharray: 1000,
                strokeDashoffset: 1000,
                animation: `arcanea-draw 1.15s ease-out forwards`,
                animationDelay: `${p.delay}s`,
              }}
            />
          ))}

          {/* nodes with subtle drift */}
          {[
            { cx: 98, cy: 320, dx: -5, dy: 3, dur: 5.3, dly: 0.1 },
            { cx: 118, cy: 284, dx: 4, dy: -4, dur: 4.6, dly: 0.4 },
            { cx: 138, cy: 250, dx: -3, dy: -3, dur: 6.1, dly: 0.2 },
            { cx: 156, cy: 220, dx: 3, dy: -4, dur: 5.2, dly: 0.6 },
            { cx: 414, cy: 320, dx: 5, dy: 3, dur: 5.0, dly: 0.15 },
            { cx: 394, cy: 284, dx: -4, dy: -4, dur: 4.8, dly: 0.45 },
            { cx: 374, cy: 250, dx: 3, dy: -3, dur: 6.2, dly: 0.25 },
            { cx: 356, cy: 220, dx: -3, dy: -4, dur: 5.4, dly: 0.55 },
            { cx: 256, cy: 164, dx: 2, dy: -2, dur: 4.9, dly: 0.35 },
            { cx: 238, cy: 360, dx: -2, dy: 3, dur: 5.7, dly: 0.2 },
            { cx: 274, cy: 360, dx: 2, dy: 3, dur: 5.9, dly: 0.3 },
            { cx: 168, cy: 410, dx: -2, dy: 2, dur: 6.6, dly: 0.15 },
            { cx: 344, cy: 410, dx: 2, dy: 2, dur: 6.4, dly: 0.22 },
          ].map((n, idx) => (
            <circle
              key={idx}
              cx={n.cx}
              cy={n.cy}
              r={Math.max(6, Math.round(strokeWidth * 0.62))}
              fill="rgba(11,18,32,0.55)"
              stroke="url(#arcanea-grad)"
              strokeWidth={Math.max(6, Math.round(strokeWidth * 0.7))}
              style={{
                transformBox: 'fill-box',
                transformOrigin: 'center',
                animation: `arcanea-twinkle 2.2s ease-in-out infinite, arcanea-drift ${n.dur}s ease-in-out infinite`,
                animationDelay: `${(idx % 6) * 0.07}s, ${n.dly}s`,
              }}
            />
          ))}
        </g>
      </svg>
    </div>
  );
};
