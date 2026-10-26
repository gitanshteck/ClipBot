import React from 'react';
import {C, F, clamp01, rand} from './theme.js';

// An L-shaped corner mark. `p` (0..1) draws it out from the corner.
export const Corner = ({x, y, size = 26, thick = 3, color = C.amber, flipX = false, flipY = false, p = 1}) => {
  const len = size * clamp01(p);
  const common = {position: 'absolute', background: color};
  return (
    <div style={{position: 'absolute', left: x, top: y, width: 0, height: 0, transform: `scale(${flipX ? -1 : 1}, ${flipY ? -1 : 1})`}}>
      <div style={{...common, left: 0, top: 0, width: len, height: thick}} />
      <div style={{...common, left: 0, top: 0, width: thick, height: len}} />
    </div>
  );
};

// four corners around a box
export const Brackets = ({x, y, w, h, size = 26, thick = 3, color = C.amber, p = 1}) => (
  <>
    <Corner x={x} y={y} size={size} thick={thick} color={color} p={p} />
    <Corner x={x + w} y={y} size={size} thick={thick} color={color} flipX p={p} />
    <Corner x={x} y={y + h} size={size} thick={thick} color={color} flipY p={p} />
    <Corner x={x + w} y={y + h} size={size} thick={thick} color={color} flipX flipY p={p} />
  </>
);

// a number drawn digit by digit in fixed-width cells, so a count-up does not jitter sideways
export const Digits = ({text, size, color = C.white, font = F.display, cell = 0.5, style = {}}) => (
  <span style={{display: 'inline-flex', fontFamily: font, fontSize: size, lineHeight: 1, color, ...style}}>
    {String(text)
      .split('')
      .map((ch, i) => (
        <span key={i} style={{display: 'inline-block', width: /\d/.test(ch) ? size * cell : 'auto', textAlign: 'center'}}>
          {ch}
        </span>
      ))}
  </span>
);

// film grain: a fixed noise field that shifts a little each frame (seeded, so a render is repeatable)
export const Grain = ({frame, opacity = 0.06}) => {
  const dx = Math.floor(rand(frame * 3 + 1) * 200);
  const dy = Math.floor(rand(frame * 3 + 2) * 200);
  return (
    <svg width="1920" height="1080" style={{position: 'absolute', inset: 0, opacity, mixBlendMode: 'overlay'}}>
      <filter id="grain" x="0" y="0" width="100%" height="100%">
        <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed={frame % 7} stitchTiles="stitch" />
        <feColorMatrix type="saturate" values="0" />
      </filter>
      <rect x={-dx} y={-dy} width="2300" height="1300" filter="url(#grain)" />
    </svg>
  );
};

// the background of a full-frame card: graphite, a fading grid, a vignette
export const CardBackground = ({frame, grid = true, glow = 0.5}) => (
  <>
    <div style={{position: 'absolute', inset: 0, background: `radial-gradient(ellipse at 50% 46%, #18212A 0%, #0B1015 52%, ${C.ink} 100%)`}} />
    {grid && (
      <div
        style={{
          position: 'absolute',
          inset: 0,
          backgroundImage: `linear-gradient(${C.grid} 1px, transparent 1px), linear-gradient(90deg, ${C.grid} 1px, transparent 1px)`,
          backgroundSize: '60px 60px',
          WebkitMaskImage: 'radial-gradient(ellipse at 50% 50%, black 10%, transparent 72%)',
          maskImage: 'radial-gradient(ellipse at 50% 50%, black 10%, transparent 72%)',
        }}
      />
    )}
    <div style={{position: 'absolute', inset: 0, background: `radial-gradient(ellipse at 50% 50%, rgba(255,179,0,${0.05 * glow}) 0%, transparent 55%)`}} />
    <div style={{position: 'absolute', inset: 0, background: 'radial-gradient(ellipse at 50% 50%, transparent 55%, rgba(0,0,0,0.6) 100%)'}} />
    <Grain frame={frame} />
  </>
);

// a row of small mono text with letter-spacing, the HUD's voice
export const Mono = ({children, size = 24, color = C.dim, track = 0.16, style = {}}) => (
  <div style={{fontFamily: F.mono, fontSize: size, color, letterSpacing: `${track}em`, textTransform: 'uppercase', whiteSpace: 'nowrap', ...style}}>{children}</div>
);

// reveals the first `p` of a string, a typewriter
export const typed = (text, p) => text.slice(0, Math.floor(text.length * clamp01(p) + 1e-6));
