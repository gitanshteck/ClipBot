import React from 'react';
import {AbsoluteFill} from 'remotion';
import {H, F, ease, lerp, prog, useClock, useFonts} from '../theme.js';
import {Digits} from '../parts.jsx';
import {HlBackground, Para, Tiny} from '../partsHl.jsx';

// The end card: what the night held, in tiles that count up, then a sign-off line. Opaque.
// props: title ("THE NIGHT IN NUMBERS"), stats [{label, value, unit?}] (up to 4), line (the sign-off), foot (a small line)
export const HlEnd = (props) => {
  useFonts();
  const {t, dur, hit, frame} = useClock(props);
  const {title = 'THE NIGHT IN NUMBERS', stats = [], line = '', foot = ''} = props;
  const out = prog(t, dur - 0.5, dur - 0.04, ease.in2);
  const head = prog(t, 0, 0.45, ease.out5);
  const n = Math.max(1, stats.length);
  const tileW = n >= 4 ? 380 : 440;
  const gap = 28;
  const total = n * tileW + (n - 1) * gap;

  return (
    <AbsoluteFill style={{opacity: 1 - out}}>
      <HlBackground t={t} frame={frame} haze={0.8} />
      <div style={{position: 'absolute', left: 0, right: 0, top: 170, display: 'flex', justifyContent: 'center', opacity: head, transform: `translateY(${(1 - head) * -24}px)`}}>
        <Para bg={H.cyan} slant={24} pad="0 60px 0 50px">
          <div style={{fontFamily: F.heavy, fontSize: 92, lineHeight: 1.18, color: H.ink, letterSpacing: '0.05em'}}>{title}</div>
        </Para>
      </div>

      <div style={{position: 'absolute', left: (1920 - total) / 2, top: 380, display: 'flex', gap}}>
        {stats.map((s, i) => {
          const a = hit + i * 0.16;
          const p = prog(t, a, a + 0.45, ease.out5);
          const count = prog(t, a + 0.05, a + 1.1, ease.out3);
          const shown = Math.round(lerp(0, Number(s.value) || 0, count));
          return (
            <div key={s.label} style={{width: tileW, opacity: Math.min(1, p * 2), transform: `translateY(${(1 - p) * 60}px)`}}>
              <Para bg="rgba(9,14,21,0.92)" slant={26} pad="0 0 0 0" style={{width: tileW}}>
                <div style={{height: 330, display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', borderTop: `6px solid ${H.cyan}`}}>
                  <div style={{display: 'flex', alignItems: 'baseline', gap: 10}}>
                    <Digits text={shown} size={190} font={F.heavy} cell={0.5} color={H.white} />
                    {s.unit && <span style={{fontFamily: F.heavy, fontSize: 64, color: H.cyan}}>{s.unit}</span>}
                  </div>
                  <Tiny size={s.label.length > 12 ? 24 : 30} color={H.cyan} track={s.label.length > 12 ? 0.18 : 0.3} style={{marginTop: 12}}>{s.label}</Tiny>
                </div>
              </Para>
            </div>
          );
        })}
      </div>

      <div style={{position: 'absolute', left: 0, right: 0, top: 800, display: 'flex', justifyContent: 'center', opacity: prog(t, hit + 0.9, hit + 1.3, ease.out2), transform: `translateY(${(1 - prog(t, hit + 0.9, hit + 1.3, ease.out3)) * 14}px)`}}>
        <div style={{fontFamily: F.heavy, fontSize: 78, color: H.white, letterSpacing: '0.08em'}}>{line}</div>
      </div>
      {foot && (
        <div style={{position: 'absolute', left: 0, right: 0, bottom: 110, display: 'flex', justifyContent: 'center', opacity: prog(t, hit + 1.1, hit + 1.5, ease.out2)}}>
          <Tiny size={26} color={H.dim} track={0.3}>{foot}</Tiny>
        </div>
      )}
    </AbsoluteFill>
  );
};
