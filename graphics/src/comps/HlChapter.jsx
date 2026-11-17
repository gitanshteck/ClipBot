import React from 'react';
import {AbsoluteFill} from 'remotion';
import {H, F, ease, pad2, prog, rand, useClock, useFonts} from '../theme.js';
import {Para, Slash, Tiny} from '../partsHl.jsx';
import {typed} from '../parts.jsx';

// The header of a chapter, laid over live footage as it starts: the chapter number in a cyan block, the title
// wiping in beside it, a rule that sweeps under, and a short typed line (what is in it).
// props: n (chapter number), title, sub (typed line, optional), x, y (px of the top-left)
export const HlChapter = (props) => {
  useFonts();
  const {t, dur, hit, frame} = useClock(props);
  const {n = 1, title = 'CHAPTER', sub = '', x = 300, y = 118} = props;

  const inP = prog(t, 0, 0.3, ease.out5);
  const reveal = prog(t, 0.1, hit + 0.2, ease.out5);
  const outP = prog(t, dur - 0.4, dur - 0.03, ease.in3);
  const rule = prog(t, 0.2, hit + 0.45, ease.out3);
  const glitch = t > hit + 0.04 && t < hit + 0.2;
  const jit = glitch ? (rand(frame) - 0.5) * 16 : 0;

  return (
    <AbsoluteFill>
      <div style={{position: 'absolute', left: x, top: y, opacity: 1 - outP, transform: `translateX(${-outP * 80}px)`}}>
        <div style={{display: 'flex', alignItems: 'stretch'}}>
          <div style={{transform: `translateX(${(1 - inP) * -60}px)`, opacity: inP, position: 'relative', zIndex: 2}}>
            <Para bg={H.cyan} slant={22} pad="0 46px 0 40px">
              <div style={{height: 108, display: 'flex', alignItems: 'center', fontFamily: F.heavy, fontSize: 84, color: H.ink, lineHeight: 1}}>{pad2(n)}</div>
            </Para>
          </div>
          <div style={{marginLeft: -16, position: 'relative'}}>
            {glitch && (
              <div style={{position: 'absolute', left: 60 + jit, top: 8, fontFamily: F.heavy, fontSize: 80, lineHeight: 1.08, color: H.cyan, opacity: 0.5, letterSpacing: '0.03em', whiteSpace: 'nowrap'}}>{title}</div>
            )}
            <Para p={reveal} slant={22} pad="0 60px 0 44px">
              <div style={{height: 108, display: 'flex', alignItems: 'center'}}>
                <div style={{fontFamily: F.heavy, fontSize: 80, lineHeight: 1.08, color: H.white, letterSpacing: '0.03em', whiteSpace: 'nowrap', textShadow: '0 3px 14px rgba(0,0,0,0.4)'}}>{title}</div>
              </div>
            </Para>
          </div>
        </div>
        <div style={{marginLeft: 70, marginTop: 8, height: 5, width: 560 * rule, background: H.cyan, boxShadow: `0 0 16px ${H.cyanSoft}`}} />
        {sub && (
          <div style={{marginLeft: 70, marginTop: 12}}>
            <div style={{display: 'inline-block', background: 'rgba(6,8,12,0.72)', padding: '4px 16px'}}>
              <Tiny size={26} color={H.white} track={0.22}>{typed(sub, prog(t, hit - 0.05, hit + 0.55, ease.linear))}</Tiny>
            </div>
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
