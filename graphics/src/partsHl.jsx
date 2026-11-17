import React from 'react';
import {H, F, clamp01, rand} from './theme.js';

// A parallelogram plate that leans like a "/" (top edge shifted right). Its width follows its content; `p` (0..1)
// wipes it in from the left. The slant is the look: every plate, chip and bar of the `hl` look shares it.
export const Para = ({bg = H.panel, slant = 22, pad = '0 46px 0 36px', p = 1, shadow = true, style = {}, children}) => (
  <div style={{display: 'inline-block', clipPath: `inset(-20px ${(1 - clamp01(p)) * 100}% -20px 0)`}}>
    <div
      style={{
        background: bg,
        clipPath: `polygon(${slant}px 0, 100% 0, calc(100% - ${slant}px) 100%, 0 100%)`,
        padding: pad,
        filter: shadow ? 'drop-shadow(0 8px 18px rgba(0,0,0,0.5))' : 'none',
        ...style,
      }}
    >
      {children}
    </div>
  </div>
);

// the same lean as a thin accent bar (the edge of a plate)
export const Slash = ({w = 12, h = 100, slant = 22, color = H.cyan, style = {}}) => (
  <div style={{width: w + slant, height: h, background: color, clipPath: `polygon(${slant}px 0, 100% 0, ${w}px 100%, 0 100%)`, ...style}} />
);

// a small tracked mono line
export const Tiny = ({children, size = 24, color = H.dim, track = 0.2, style = {}}) => (
  <div style={{fontFamily: F.mono, fontSize: size, color, letterSpacing: `${track}em`, textTransform: 'uppercase', whiteSpace: 'nowrap', ...style}}>{children}</div>
);

// a crosshair, drawn in SVG so it stays sharp at any scale
export const Crosshair = ({size = 56, color = H.hot, thick = 4, p = 1}) => {
  const c = size / 2;
  const r = size * 0.3;
  const gap = size * 0.12;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{opacity: p, transform: `rotate(${(1 - p) * 90}deg) scale(${0.6 + 0.4 * p})`}}>
      <circle cx={c} cy={c} r={r} fill="none" stroke={color} strokeWidth={thick} />
      <path d={`M${c} 0 V${c - gap} M${c} ${size} V${c + gap} M0 ${c} H${c - gap} M${size} ${c} H${c + gap}`} stroke={color} strokeWidth={thick} strokeLinecap="square" />
      <circle cx={c} cy={c} r={thick * 0.7} fill={color} />
    </svg>
  );
};

// light grain + scanlines for the opaque cards
export const Scan = ({frame, opacity = 0.05}) => {
  const dy = Math.floor(rand(frame * 3 + 2) * 6);
  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        opacity,
        backgroundImage: 'repeating-linear-gradient(0deg, rgba(255,255,255,0.9) 0px, rgba(255,255,255,0.9) 1px, transparent 1px, transparent 4px)',
        backgroundPositionY: dy,
        mixBlendMode: 'overlay',
      }}
    />
  );
};

// the opaque card background: ink, a cyan haze, diagonal beams that drift, a vignette
export const HlBackground = ({t, frame, haze = 1}) => {
  const beams = [0, 1, 2, 3, 4].map((i) => {
    const x = ((i * 480 + t * (40 + i * 14)) % 2600) - 340;
    return (
      <div
        key={i}
        style={{
          position: 'absolute',
          left: x,
          top: -200,
          width: 70 + i * 24,
          height: 1500,
          background: `linear-gradient(180deg, transparent 0%, rgba(53,208,255,${0.05 + 0.02 * i}) 40%, transparent 100%)`,
          transform: 'rotate(18deg)',
        }}
      />
    );
  });
  return (
    <>
      <div style={{position: 'absolute', inset: 0, background: `radial-gradient(ellipse at 50% 48%, #0F1C28 0%, #080C12 55%, ${H.ink} 100%)`}} />
      <div style={{position: 'absolute', inset: 0, background: `radial-gradient(ellipse at 50% 50%, rgba(53,208,255,${0.10 * haze}) 0%, transparent 58%)`}} />
      <div style={{position: 'absolute', inset: 0, overflow: 'hidden'}}>{beams}</div>
      <div style={{position: 'absolute', inset: 0, background: 'radial-gradient(ellipse at 50% 50%, transparent 52%, rgba(0,0,0,0.65) 100%)'}} />
      <Scan frame={frame} />
    </>
  );
};
