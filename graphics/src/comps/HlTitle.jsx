import React from 'react';
import {AbsoluteFill} from 'remotion';
import {H, F, ease, prog, rand, useClock, useFonts} from '../theme.js';
import {HlBackground, Tiny} from '../partsHl.jsx';
import {typed} from '../parts.jsx';

// The series card: an opaque plate that builds to a slam on `hit`.
// props: series ("TECK HIGHLIGHTS": the first word is big and white, the rest cyan), kicker (typed line above),
//        sub (line under the rule), tag (a cyan block, e.g. "DAY ONE")
export const HlTitle = (props) => {
  useFonts();
  const {t, dur, hit, frame} = useClock(props);
  const {series = 'TECK HIGHLIGHTS', kicker = '', sub = '', tag = ''} = props;
  const words = series.split(' ');
  const first = words.shift();
  const rest = words.join(' ');

  const since = t - hit;
  const slam = since >= 0 ? prog(t, hit, hit + 0.2, ease.out5) : 0;
  const scale = since >= 0 ? 1 + 0.24 * (1 - slam) : 1.24;
  const blur = since >= 0 ? 18 * (1 - slam) : 18;
  const op = since >= 0 ? Math.min(1, 0.2 + slam) : 0;
  const shake = since >= 0 && since < 0.18 ? (1 - since / 0.18) * 12 : 0;
  const flash = since >= 0 ? (1 - prog(t, hit, hit + 0.2, ease.out2)) * 0.5 : 0;
  const split = since >= 0 && since < 0.22 ? (1 - since / 0.22) * 18 : 0;
  const kick = prog(t, 0.15, 0.8, ease.linear);
  const rule = prog(t, hit + 0.05, hit + 0.55, ease.out3);
  const subP = prog(t, hit + 0.35, hit + 0.8, ease.out2);
  const tagP = prog(t, hit + 0.1, hit + 0.45, ease.back);
  const out = prog(t, dur - 0.24, dur - 0.02, ease.in2);

  const big = (color, dx, dy, op2 = 1, extra = {}) => (
    <div style={{position: 'absolute', left: 0, right: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', opacity: op2 * op, transform: `translate(${dx}px, ${dy}px)`, ...extra}}>
      <div style={{fontFamily: F.heavy, fontSize: 400, lineHeight: 1, color, letterSpacing: '0.03em'}}>{first}</div>
    </div>
  );

  return (
    <AbsoluteFill style={{opacity: 1 - out * 0.9}}>
      <HlBackground t={t} frame={frame} />
      <div style={{position: 'absolute', left: 120, right: 120, top: 96, height: 3, background: H.cyanLine, transformOrigin: 'left', transform: `scaleX(${prog(t, 0, 0.5, ease.out3)})`}} />
      <div style={{position: 'absolute', left: 120, right: 120, bottom: 96, height: 3, background: H.cyanLine, transformOrigin: 'right', transform: `scaleX(${prog(t, 0.1, 0.6, ease.out3)})`}} />

      <div style={{position: 'absolute', left: 0, right: 0, top: 236, display: 'flex', justifyContent: 'center'}}>
        <Tiny size={32} color={H.cyan} track={0.5}>{typed(kicker, kick)}</Tiny>
      </div>

      <div style={{position: 'absolute', left: 0, right: 0, top: 270, height: 420, filter: `blur(${blur}px)`, transform: `translate(${(rand(frame) - 0.5) * shake}px, ${(rand(frame + 9) - 0.5) * shake}px) scale(${scale})`}}>
        {split > 0 && big(H.cyan, -split, 0, 0.7)}
        {split > 0 && big(H.hot, split, 0, 0.6)}
        {big(H.white, 0, 0)}
      </div>

      <div style={{position: 'absolute', left: 0, right: 0, top: 672, display: 'flex', justifyContent: 'center', opacity: op, transform: `scale(${1 + (scale - 1) * 0.5})`}}>
        <div style={{fontFamily: F.heavy, fontSize: 168, lineHeight: 1, color: H.cyan, letterSpacing: '0.12em', textShadow: `0 0 40px ${H.cyanSoft}`}}>{rest}</div>
      </div>

      <div style={{position: 'absolute', left: '50%', top: 872, width: 980 * rule, height: 5, marginLeft: -490 * rule, background: H.cyan}} />
      <div style={{position: 'absolute', left: 0, right: 0, top: 892, display: 'flex', justifyContent: 'center', opacity: subP, transform: `translateY(${(1 - subP) * 12}px)`}}>
        <Tiny size={32} color={H.white} track={0.36}>{sub}</Tiny>
      </div>

      {tag && (
        <div style={{position: 'absolute', right: 160, top: 150, transform: `scale(${tagP}) rotate(${(1 - tagP) * -8}deg)`, opacity: tagP}}>
          <div style={{background: H.cyan, color: H.ink, fontFamily: F.heavy, fontSize: 54, letterSpacing: '0.08em', padding: '4px 26px 2px 26px', clipPath: 'polygon(14px 0, 100% 0, calc(100% - 14px) 100%, 0 100%)'}}>{tag}</div>
        </div>
      )}
      <div style={{position: 'absolute', inset: 0, background: '#fff', opacity: flash}} />
    </AbsoluteFill>
  );
};
