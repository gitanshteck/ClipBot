import React from 'react';
import {AbsoluteFill} from 'remotion';
import {H, F, ease, prog, useClock, useFonts} from '../theme.js';
import {Para, Slash, Tiny} from '../partsHl.jsx';

// A short label over live footage: the gag, the quote, the name of what you are watching. Pops in on `hit`.
// props: text, sub (a small line under it, optional), tone ("cyan"|"gold"|"hot"), x, y (px of the top-left), align ("left"|"center")
const TONE = {cyan: H.cyan, gold: H.gold, hot: H.hot};

export const HlTag = (props) => {
  useFonts();
  const {t, dur, hit} = useClock(props);
  const {text = '', sub = '', tone = 'cyan', x = 300, y = 120, align = 'left'} = props;
  const accent = TONE[tone] || H.cyan;

  const inP = prog(t, 0, 0.24, ease.out5);
  const pop = t >= hit ? prog(t, hit, hit + 0.28, ease.back) : 0;
  const outP = prog(t, dur - 0.34, dur - 0.03, ease.in3);
  const scale = t >= hit ? 1 + 0.12 * (1 - pop) : 1.12;

  const box = (
    <div style={{display: 'inline-flex', flexDirection: 'column', alignItems: 'flex-start', opacity: 1 - outP, transform: `translateY(${-outP * 22}px)`}}>
      <div style={{display: 'flex', alignItems: 'stretch', transform: `translateX(${(1 - inP) * -70}px)`, opacity: Math.min(1, inP * 2)}}>
        <Slash w={12} h={74} slant={16} color={accent} style={{marginRight: -2}} />
        <Para p={inP} slant={16} pad="0 42px 0 30px">
          <div style={{height: 74, display: 'flex', alignItems: 'center'}}>
            <div style={{fontFamily: F.heavy, fontSize: 56, lineHeight: 1.1, color: H.white, letterSpacing: '0.04em', whiteSpace: 'nowrap', transform: `scale(${scale})`, transformOrigin: 'left center'}}>{text}</div>
          </div>
        </Para>
      </div>
      {sub && (
        <div style={{marginLeft: 34, marginTop: 6, opacity: prog(t, hit, hit + 0.25, ease.out2)}}>
          <div style={{display: 'inline-block', background: 'rgba(6,8,12,0.72)', padding: '2px 14px'}}>
            <Tiny size={24} color={accent} track={0.24}>{sub}</Tiny>
          </div>
        </div>
      )}
    </div>
  );
  return (
    <AbsoluteFill>
      {align === 'center' ? (
        <div style={{position: 'absolute', left: 0, right: 0, top: y, display: 'flex', justifyContent: 'center'}}>{box}</div>
      ) : (
        <div style={{position: 'absolute', left: x, top: y}}>{box}</div>
      )}
    </AbsoluteFill>
  );
};
