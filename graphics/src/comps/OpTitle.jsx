import React from 'react';
import {AbsoluteFill} from 'remotion';
import {C, F, ease, pad2, prog, rand, useClock, useFonts} from '../theme.js';
import {Mono, typed} from '../parts.jsx';

// The header of an operation, laid over live footage at the start of a chapter.
// props: op (number), title, sub (a mono line under it), x, y (top-left of the stack, px), label ("OPERATION")
export const OpTitle = (props) => {
  useFonts();
  const {t, dur, hit, frame} = useClock(props);
  const {op = 1, title = 'FIRST CONTACT', sub = '', label = 'OPERATION', x = 72, y = 84} = props;

  const scrim = prog(t, 0, 0.4, ease.out2) * (1 - prog(t, dur - 0.5, dur - 0.05, ease.in2));
  const exit = prog(t, dur - 0.42, dur - 0.04, ease.in3);
  const reveal = prog(t, 0.08, hit + 0.18, ease.out5);
  const slide = (1 - reveal) * 70;
  const rule = prog(t, 0.05, 0.62, ease.out3);
  const tagP = prog(t, 0, 0.25, ease.out3);
  const glitching = t > hit + 0.1 && t < hit + 0.26;
  const jitter = glitching ? (rand(frame) - 0.5) * 14 : 0;

  return (
    <AbsoluteFill>
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: 0,
          width: 1250,
          height: 560,
          opacity: scrim,
          background: 'linear-gradient(100deg, rgba(3,5,7,0.78) 0%, rgba(3,5,7,0.55) 55%, rgba(3,5,7,0) 100%)',
        }}
      />
      <div style={{position: 'absolute', left: x, top: y, opacity: 1 - exit, transform: `translateX(${-exit * 60}px)`}}>
        <div style={{display: 'flex', alignItems: 'center', gap: 16, opacity: tagP, transform: `translateX(${(1 - tagP) * -24}px)`}}>
          <div style={{fontFamily: F.mono, fontSize: 30, letterSpacing: '0.12em', color: C.ink, background: C.amber, padding: '4px 14px'}}>
            OP {pad2(op)}
          </div>
          <Mono size={26} color={C.dim} track={0.3}>{label}</Mono>
        </div>

        <div style={{position: 'relative', marginTop: 8, height: 190}}>
          {glitching && (
            <>
              <div style={{position: 'absolute', left: jitter, top: 0, fontFamily: F.display, fontSize: 184, lineHeight: 1.04, color: C.amber, opacity: 0.55, letterSpacing: '0.02em', whiteSpace: 'nowrap'}}>{title}</div>
              <div style={{position: 'absolute', left: -jitter, top: 0, fontFamily: F.display, fontSize: 184, lineHeight: 1.04, color: '#3CD6FF', opacity: 0.4, letterSpacing: '0.02em', whiteSpace: 'nowrap'}}>{title}</div>
            </>
          )}
          <div
            style={{
              position: 'absolute',
              left: slide,
              top: 0,
              fontFamily: F.display,
              fontSize: 184,
              lineHeight: 1.04,
              color: C.white,
              letterSpacing: '0.02em',
              whiteSpace: 'nowrap',
              clipPath: `inset(0 ${(1 - reveal) * 100}% 0 0)`,
              textShadow: '0 4px 24px rgba(0,0,0,0.55)',
            }}
          >
            {title}
          </div>
        </div>

        <div style={{height: 5, width: 640 * rule, background: C.amber, marginTop: 2, boxShadow: `0 0 18px ${C.amberSoft}`}} />
        {sub && (
          <div style={{marginTop: 16}}>
            <Mono size={30} color={C.white} track={0.2}>{typed(sub, prog(t, hit - 0.05, hit + 0.55, ease.linear))}</Mono>
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
