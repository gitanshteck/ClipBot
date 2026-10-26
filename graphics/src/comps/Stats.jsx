import React from 'react';
import {AbsoluteFill} from 'remotion';
import {C, F, ease, pad2, prog, useClock, useFonts} from '../theme.js';
import {Brackets, CardBackground, Digits, Mono} from '../parts.jsx';

// The debrief: the numbers of what the video contained, counting up, and the operations ticked off.
// props: title, stats [{label, value, unit, decimals}], ops [{n, name, count}], foot,
//        next (a label over the empty frames where YouTube's end-screen elements go; "" for none)
export const Stats = (props) => {
  useFonts();
  const {t, dur, hit, frame} = useClock(props);
  const {title = 'MISSION COMPLETE', stats = [], ops = [], foot = '', next = 'NEXT REPORT'} = props;

  const frameP = prog(t, 0, 0.5, ease.out3);
  const head = prog(t, 0.05, 0.55, ease.out5);
  const stamp = prog(t, hit, hit + 0.3, ease.back);
  const out = prog(t, dur - 0.3, dur - 0.02, ease.in2);
  const endP = prog(t, 1.2, 1.8, ease.out3);

  return (
    <AbsoluteFill style={{opacity: 1 - out}}>
      <CardBackground frame={frame} glow={0.8} />
      <Brackets x={64} y={56} w={1792} h={968} size={70} thick={3} p={frameP} />

      <div style={{position: 'absolute', left: 130, top: 96}}>
        <Mono size={26} color={C.amber} track={0.5}>{foot}</Mono>
        <div style={{position: 'relative', marginTop: 4, height: 168, clipPath: `inset(0 ${(1 - head) * 100}% 0 0)`}}>
          <div style={{fontFamily: F.heavy, fontSize: 168, lineHeight: 1.05, color: C.white, letterSpacing: '0.03em', whiteSpace: 'nowrap'}}>{title}</div>
        </div>
        <div style={{height: 5, width: 860 * prog(t, 0.2, 0.8, ease.out3), background: C.amber, marginTop: 6}} />
      </div>

      <div
        style={{
          position: 'absolute',
          left: 1590,
          top: 104,
          width: 190,
          height: 190,
          border: `5px solid ${C.amber}`,
          borderRadius: 10,
          transform: `rotate(-9deg) scale(${stamp > 0 ? 1.35 - 0.35 * stamp : 0})`,
          opacity: stamp,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontFamily: F.heavy,
          fontSize: 104,
          color: C.amber,
        }}
      >
        OK
      </div>

      <div style={{position: 'absolute', left: 130, top: 372, width: 1180, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '28px 30px'}}>
        {stats.map((s, i) => {
          const start = hit + 0.15 + i * 0.22;
          const rise = prog(t, start - 0.12, start + 0.2, ease.out3);
          const v = s.value * prog(t, start, start + 0.95, ease.out3);
          const decimals = s.decimals || 0;
          const text = decimals ? v.toFixed(decimals) : String(Math.round(v));
          return (
            <div key={s.label} style={{borderTop: `2px solid ${C.amberLine}`, paddingTop: 12, opacity: rise, transform: `translateY(${(1 - rise) * 22}px)`}}>
              <Mono size={22} color={C.amber} track={0.26}>{s.label}</Mono>
              <div style={{display: 'flex', alignItems: 'baseline', marginTop: 4}}>
                <Digits text={text} size={150} cell={0.47} color={C.white} />
                {s.unit && <span style={{fontFamily: F.display, fontSize: 64, color: C.amber, marginLeft: 10}}>{s.unit}</span>}
              </div>
            </div>
          );
        })}
      </div>

      <div style={{position: 'absolute', left: 1400, top: 372, width: 420}}>
        <Mono size={22} color={C.amber} track={0.3}>OPERATIONS</Mono>
        <div style={{height: 2, background: C.amberLine, margin: '12px 0 8px 0'}} />
        {ops.map((o, i) => {
          const p = prog(t, hit + 0.3 + i * 0.28, hit + 0.55 + i * 0.28, ease.out3);
          return (
            <div key={o.n} style={{display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', padding: '9px 0', opacity: p, transform: `translateX(${(1 - p) * 26}px)`, borderBottom: '1px solid rgba(255,255,255,0.08)'}}>
              <div style={{display: 'flex', gap: 16, alignItems: 'baseline'}}>
                <Mono size={24} color={C.amber} track={0.1}>{pad2(o.n)}</Mono>
                <div style={{fontFamily: F.display, fontSize: 38, color: C.white, letterSpacing: '0.05em'}}>{o.name}</div>
              </div>
              <Mono size={26} color={C.dim} track={0.1}>{o.count}</Mono>
            </div>
          );
        })}
      </div>

      {next ? (
        <div style={{position: 'absolute', left: 130, bottom: 96, opacity: endP}}>
          <Mono size={22} color={C.dim} track={0.4}>{next} {'>>'}</Mono>
        </div>
      ) : null}
    </AbsoluteFill>
  );
};
