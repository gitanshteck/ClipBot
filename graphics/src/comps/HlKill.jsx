import React from 'react';
import {AbsoluteFill} from 'remotion';
import {H, F, ease, pad2, prog, useClock, useFonts} from '../theme.js';
import {Crosshair, Para, Slash, Tiny} from '../partsHl.jsx';

// A plate for one kill, in the game's own words: the line the game printed (KILL, HEADSHOT KILL, MELEE KILL), the
// medals it stacked under it, and - when this kill made one - the multi-kill banner. Lands on the kill (`hit`).
// props: kind ("KILL"|"HEADSHOT KILL"|"MELEE KILL"), tags ["QUICK SCOPE", ...] (the game's medal lines, at most 3),
//        multi ("DOUBLE KILL" or null), index, total, x (px from the left), bottom (px from the bottom edge: the stack grows upward)
export const HlKill = (props) => {
  useFonts();
  const {t, dur, hit} = useClock(props);
  const {kind = 'KILL', tags = [], multi = null, index = 1, total = null, x = 64, bottom = 90} = props;
  const head = /HEADSHOT/.test(kind);
  const accent = head ? H.hot : H.cyan;

  const inP = prog(t, 0, 0.26, ease.out5);
  const outP = prog(t, dur - 0.38, dur - 0.03, ease.in3);
  const land = t >= hit ? prog(t, hit, hit + 0.3, ease.back) : 0;
  const flash = t >= hit ? 1 - prog(t, hit, hit + 0.18, ease.out2) : 0;
  const scale = t >= hit ? 1 + 0.22 * (1 - land) : 1.22;
  const label = total ? `KILL ${pad2(index)} / ${pad2(total)}` : `KILL ${pad2(index)}`;

  return (
    <AbsoluteFill>
      <div style={{position: 'absolute', left: x, bottom, opacity: 1 - outP, transform: `translateX(${-outP * 70}px)`}}>
        {multi && (
          <div style={{marginBottom: 10, transform: `translateX(${(1 - prog(t, hit - 0.05, hit + 0.25, ease.out5)) * -90}px)`, opacity: prog(t, hit - 0.05, hit + 0.12, ease.out2)}}>
            <Para bg={H.gold} slant={20} pad="2px 40px 0 30px">
              <div style={{fontFamily: F.heavy, fontSize: 58, lineHeight: 1.12, color: H.ink, letterSpacing: '0.04em'}}>{multi}</div>
            </Para>
          </div>
        )}

        <div style={{position: 'relative', display: 'inline-block'}}>
          <div style={{position: 'absolute', left: 0, top: 0, bottom: 0, zIndex: 2, opacity: inP}}>
            <Slash w={12} h={132} slant={22} color={accent} />
          </div>
          <div style={{marginLeft: 6}}>
            <Para p={inP} pad="0 52px 0 50px" slant={22}>
              <div style={{height: 132, display: 'flex', flexDirection: 'column', justifyContent: 'center', position: 'relative'}}>
                <Tiny size={22} color={accent} track={0.28} style={{marginBottom: 2}}>{label}</Tiny>
                <div style={{display: 'flex', alignItems: 'center', gap: 18}}>
                  <div
                    style={{
                      fontFamily: F.heavy,
                      fontSize: 84,
                      lineHeight: 1.04,
                      color: head ? H.hot : H.white,
                      letterSpacing: '0.03em',
                      whiteSpace: 'nowrap',
                      transform: `scale(${scale})`,
                      transformOrigin: 'left center',
                      textShadow: '0 3px 14px rgba(0,0,0,0.4)',
                    }}
                  >
                    {kind}
                  </div>
                  {head && <Crosshair size={58} color={H.hot} p={land} />}
                </div>
                <div style={{position: 'absolute', inset: 0, background: '#fff', opacity: flash * 0.22}} />
              </div>
            </Para>
          </div>
        </div>

        <div style={{display: 'flex', flexDirection: 'column', gap: 8, marginTop: 10, marginLeft: 40, alignItems: 'flex-start'}}>
          {tags.map((tag, i) => {
            const p = prog(t, hit + 0.06 + i * 0.09, hit + 0.34 + i * 0.09, ease.out5);
            return (
              <div key={tag} style={{opacity: Math.min(1, p * 2), transform: `translateX(${(1 - p) * -120}px)`}}>
                <Para bg="rgba(8,12,18,0.9)" slant={14} pad="0 30px 0 24px" shadow={false}>
                  <div style={{display: 'flex', alignItems: 'center', gap: 12, height: 46}}>
                    <div style={{width: 8, height: 26, background: H.gold, transform: 'skewX(-14deg)'}} />
                    <div style={{fontFamily: F.display, fontSize: 38, lineHeight: 1, color: H.gold, letterSpacing: '0.06em', whiteSpace: 'nowrap'}}>{tag}</div>
                  </div>
                </Para>
              </div>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};
