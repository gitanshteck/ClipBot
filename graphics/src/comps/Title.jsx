import React from 'react';
import {AbsoluteFill} from 'remotion';
import {C, F, ease, pad2, prog, rand, useClock, useFonts} from '../theme.js';
import {Brackets, CardBackground, Mono, typed} from '../parts.jsx';

// The series card: a full-frame opaque plate that builds to a slam on `hit`.
// props: series ("AFTER ACTION", the last word is set in amber), number (1), kicker (the typed line above),
//        sub (the line under the rule), foot (the loading line at the bottom)
export const Title = (props) => {
  useFonts();
  const {t, dur, hit, frame} = useClock(props);
  const {series = 'AFTER ACTION', number = 1, kicker = 'FIELD REPORT', sub = '', foot = 'LOADING OPERATIONS', meta = []} = props;
  const words = series.split(' ');
  const last = words.pop();
  const head = words.join(' ');

  const frameP = prog(t, 0, 0.55, ease.out3);
  const kick = prog(t, 0.2, 0.85, ease.linear);
  const since = t - hit;
  const slam = since >= 0 ? prog(t, hit, hit + 0.22, ease.out5) : 0;
  const scale = since >= 0 ? 1 + 0.2 * (1 - slam) : 1.2;
  const blur = since >= 0 ? 16 * (1 - slam) : 16;
  const titleOp = since >= 0 ? Math.min(1, 0.25 + slam) : 0;
  const shake = since >= 0 && since < 0.2 ? (1 - since / 0.2) * 11 : 0;
  const flash = since >= 0 ? (1 - prog(t, hit, hit + 0.22, ease.out2)) * 0.42 : 0;
  const rule = prog(t, hit + 0.04, hit + 0.55, ease.out3);
  const subP = prog(t, hit + 0.4, hit + 0.9, ease.out2);
  const out = prog(t, dur - 0.26, dur - 0.02, ease.in2);
  const bar = prog(t, 0.25, hit + 1.3, ease.inOut3);
  const ghost = prog(t, hit, hit + 0.6, ease.out3);
  const sweepY = 140 + ((t * 700) % 880);

  return (
    <AbsoluteFill style={{opacity: 1 - out * 0.9, transform: `scale(${1 + out * 0.03})`}}>
      <CardBackground frame={frame} />
      <div style={{position: 'absolute', left: 0, right: 0, top: sweepY, height: 2, background: 'rgba(255,179,0,0.10)'}} />
      <Brackets x={80} y={72} w={1760} h={936} size={90} thick={3} p={frameP} />

      {number != null && (
        <div
          style={{
            position: 'absolute',
            right: 150,
            top: 180,
            fontFamily: F.display,
            fontSize: 720,
            lineHeight: 1,
            color: 'transparent',
            WebkitTextStroke: `2px rgba(255,179,0,${0.18 * ghost})`,
            letterSpacing: '0.02em',
            transform: `translateX(${(1 - ghost) * 80}px)`,
          }}
        >
          {pad2(number)}
        </div>
      )}

      <div style={{position: 'absolute', left: 150, top: 150, display: 'flex', flexDirection: 'column', gap: 10}}>
        {meta.map((line, i) => (
          <Mono key={i} size={22} color={i === 0 ? C.amber : C.dim} track={0.25}>
            {typed(line, prog(t, 0.25 + i * 0.22, 0.7 + i * 0.22, ease.linear))}
          </Mono>
        ))}
      </div>

      <div style={{position: 'absolute', left: 0, right: 0, top: 322, display: 'flex', justifyContent: 'center'}}>
        <Mono size={30} color={C.amber} track={0.5}>{typed(kicker, kick)}</Mono>
      </div>

      <div
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          top: 372,
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'baseline',
          gap: 44,
          opacity: titleOp,
          transform: `translate(${(rand(frame) - 0.5) * shake}px, ${(rand(frame + 9) - 0.5) * shake}px) scale(${scale})`,
          filter: `blur(${blur}px)`,
        }}
      >
        {head && <span style={{fontFamily: F.heavy, fontSize: 300, lineHeight: 1.05, color: C.white, letterSpacing: '0.03em'}}>{head}</span>}
        <span style={{fontFamily: F.heavy, fontSize: 300, lineHeight: 1.05, color: C.amber, letterSpacing: '0.03em'}}>{last}</span>
      </div>

      <div style={{position: 'absolute', left: '50%', top: 726, width: 1120 * rule, height: 5, marginLeft: -560 * rule, background: C.amber}} />
      <div style={{position: 'absolute', left: 0, right: 0, top: 754, display: 'flex', justifyContent: 'center', opacity: subP, transform: `translateY(${(1 - subP) * 14}px)`}}>
        <Mono size={34} color={C.white} track={0.34}>{sub}</Mono>
      </div>

      <div style={{position: 'absolute', left: 150, bottom: 118, width: 620}}>
        <div style={{display: 'flex', justifyContent: 'space-between'}}>
          <Mono size={20} color={C.dim} track={0.3}>{foot}</Mono>
          <Mono size={20} color={C.amber} track={0.2}>{pad2(bar * 100)}%</Mono>
        </div>
        <div style={{height: 4, background: 'rgba(255,255,255,0.1)', marginTop: 10}}>
          <div style={{height: 4, width: `${bar * 100}%`, background: C.amber}} />
        </div>
      </div>

      <div style={{position: 'absolute', inset: 0, background: '#fff', opacity: flash}} />
    </AbsoluteFill>
  );
};
