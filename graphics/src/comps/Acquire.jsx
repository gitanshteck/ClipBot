import React from 'react';
import {AbsoluteFill} from 'remotion';
import {C, F, ease, prog, rand, useClock, useFonts} from '../theme.js';
import {Corner, Digits, Mono} from '../parts.jsx';

// The tease for a shot the video is saving: a range finder that is still working. The digits scramble and never
// settle - nothing here claims a number - and the marker sweeps the ruler looking for the target.
// props: label ("TARGET ACQUIRED"), line ("SNIPER OVERWATCH"), x, y
export const Acquire = (props) => {
  useFonts();
  const {t, dur, hit, frame} = useClock(props);
  const {label = 'TARGET ACQUIRED', line = 'SNIPER OVERWATCH', x = 64, y = 72} = props;
  const W = 600;

  const inP = prog(t, 0, 0.34, ease.out5);
  const outP = prog(t, dur - 0.3, dur - 0.04, ease.in3);
  const blink = Math.floor(t * 3) % 2 === 0;
  // a new digit every frame: it reads as a number being worked out, not as a reading
  const digit = (i) => Math.floor(rand(Math.floor(frame / 2) * 7 + i * 13) * 10);
  const markerX = (0.5 + 0.5 * Math.sin(t * 2.3)) * (W - 70);
  const lockP = prog(t, hit, hit + 0.3, ease.out2);

  return (
    <AbsoluteFill>
      <div style={{position: 'absolute', left: x, top: y, width: W, opacity: 1 - outP, transform: `translateX(${-outP * 40}px)`}}>
        <div style={{position: 'absolute', inset: 0, background: C.panel, border: `1px solid ${C.amberLine}`, clipPath: `inset(0 ${(1 - inP) * 100}% 0 0)`}} />
        <div style={{position: 'absolute', left: 0, top: 0, width: 7, height: '100%', background: C.red, transformOrigin: 'top', transform: `scaleY(${inP})`}} />
        <Corner x={-1} y={-1} size={24} thick={3} color={C.red} p={inP} />
        <Corner x={W + 1} y={-1} size={24} thick={3} color={C.red} p={inP} flipX />
        <div style={{position: 'relative', padding: '20px 30px 22px 38px', clipPath: `inset(0 ${(1 - inP) * 100}% 0 0)`}}>
          <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 28}}>
            <Mono size={22} color={C.red} track={0.14}>
              <span style={{opacity: blink ? 1 : 0.15}}>{'●'}</span> {label}
            </Mono>
            <Mono size={18} color={C.dim} track={0.14}>{line}</Mono>
          </div>
          <div style={{height: 1, background: C.amberLine, margin: '12px 0 8px 0'}} />
          <div style={{display: 'flex', alignItems: 'baseline'}}>
            <Digits text={`${digit(0)}${digit(1)}${digit(2)}`} size={150} cell={0.46} color={C.amber} />
            <span style={{fontFamily: F.display, fontSize: 62, color: C.dim, marginLeft: 10}}>M</span>
          </div>
          <div style={{position: 'relative', height: 40, marginTop: 6, width: W - 70}}>
            <div style={{position: 'absolute', left: 0, bottom: 0, width: W - 70, height: 2, background: 'rgba(255,255,255,0.2)'}} />
            {Array.from({length: 31}).map((_, i) => (
              <div key={i} style={{position: 'absolute', left: (i / 30) * (W - 70), bottom: 0, width: 2, height: i % 10 === 0 ? 20 : i % 5 === 0 ? 13 : 7, background: 'rgba(255,255,255,0.3)'}} />
            ))}
            <div style={{position: 'absolute', left: markerX - 7, bottom: 20, width: 0, height: 0, borderLeft: '7px solid transparent', borderRight: '7px solid transparent', borderTop: `11px solid ${C.amber}`}} />
          </div>
          <div style={{marginTop: 14, opacity: lockP}}>
            <Mono size={22} color={C.white} track={0.3}>CALCULATING SOLUTION{'.'.repeat(1 + (Math.floor(t * 4) % 3))}</Mono>
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};
