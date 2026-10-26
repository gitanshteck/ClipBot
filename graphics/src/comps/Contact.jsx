import React from 'react';
import {AbsoluteFill} from 'remotion';
import {C, F, ease, pad2, prog, useClock, useFonts} from '../theme.js';
import {Corner, Digits, Mono} from '../parts.jsx';

const TONES = {kill: C.amber, head: C.red, nade: '#FF7A1A', multi: '#FFD866', long: C.amber};
const CHIP = {HEADSHOT: C.red, NADE: '#FF7A1A', 'THROUGH COVER': C.amber};

// A range ruler: a tick every 10 m, taller every 50 and 100, and a marker that rides the count to the lock.
const Ruler = ({value, max, width, accent, locked, p}) => {
  const ticks = [];
  for (let m = 0; m <= max; m += 10) {
    const h = m % 100 === 0 ? 20 : m % 50 === 0 ? 13 : 7;
    ticks.push(<div key={m} style={{position: 'absolute', left: (m / max) * width, bottom: 0, width: 2, height: h, background: m <= value ? accent : 'rgba(255,255,255,0.28)'}} />);
  }
  const labels = [];
  for (let m = 0; m <= max; m += 100) {
    labels.push(
      <div key={m} style={{position: 'absolute', left: (m / max) * width - 20, top: 28, width: 40, textAlign: 'center', fontFamily: F.mono, fontSize: 16, color: C.dim, letterSpacing: '0.1em'}}>
        {m}
      </div>
    );
  }
  const x = Math.min(1, value / max) * width;
  return (
    <div style={{position: 'relative', height: 50, marginTop: 10, width, opacity: p}}>
      <div style={{position: 'absolute', left: 0, bottom: 0, width, height: 2, background: 'rgba(255,255,255,0.2)'}} />
      <div style={{position: 'absolute', left: 0, bottom: 0, width: x, height: 2, background: accent}} />
      {ticks}
      <div style={{position: 'absolute', left: x - 7, bottom: 22, width: 0, height: 0, borderLeft: '7px solid transparent', borderRight: '7px solid transparent', borderTop: `11px solid ${locked ? '#fff' : accent}`}} />
      {labels}
    </div>
  );
};

// A contact report on a kill: index, range counting up to a lock on the hit, who, with what, and chips.
// props: index, total, distance (metres, or null when the feed did not give one), headline, tone,
//        victim, icon ("file:<png>"), tags ["HEADSHOT"...], x, y (px of the top-left corner)
export const Contact = (props) => {
  useFonts();
  const {t, dur, hit, frame} = useClock(props);
  const {index = 1, total = null, distance = null, headline = 'KILL CONFIRMED', tone = 'kill', victim = '', icon = null,
    tags = [], x = 64, y = 72, hero = false} = props;
  const accent = TONES[tone] || C.amber;
  const W = hero ? 760 : 640;

  const inP = prog(t, 0, 0.34, ease.out5);
  const outP = prog(t, dur - 0.42, dur - 0.04, ease.in3);
  const count = prog(t, 0.06, hit, ease.out3);
  const lock = prog(t, hit, hit + 0.38, ease.back);
  const flash = t >= hit ? 1 - prog(t, hit, hit + 0.2, ease.out2) : 0;
  const shown = distance == null ? null : Math.round(distance * count);
  const numScale = t >= hit ? 1 + 0.16 * (1 - lock) : 1;
  const numSize = hero ? 190 : 140;

  const idx = total ? `${pad2(index)} / ${pad2(total)}` : pad2(index);

  return (
    <AbsoluteFill>
      <div style={{position: 'absolute', left: x, top: y, width: W, opacity: 1 - outP, transform: `translateX(${-outP * 44}px)`}}>
        <div
          style={{
            position: 'absolute',
            inset: 0,
            background: C.panel,
            border: `1px solid ${C.amberLine}`,
            clipPath: `inset(0 ${(1 - inP) * 100}% 0 0)`,
          }}
        />
        <div style={{position: 'absolute', left: 0, top: 0, width: 7, height: '100%', background: accent, transformOrigin: 'top', transform: `scaleY(${inP})`}} />
        <div style={{position: 'absolute', inset: 0, background: '#fff', opacity: flash * 0.16, clipPath: `inset(0 ${(1 - inP) * 100}% 0 0)`}} />
        <Corner x={-1} y={-1} size={24} thick={3} color={accent} p={inP} />
        <Corner x={W + 1} y={-1} size={24} thick={3} color={accent} p={inP} flipX />

        <div style={{position: 'relative', padding: '20px 30px 22px 38px', clipPath: `inset(0 ${(1 - inP) * 100}% 0 0)`}}>
          <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'baseline'}}>
            <Mono size={24} color={accent} track={0.2}>CONTACT {idx}</Mono>
            <Mono size={22} color={C.dim} track={0.2}>{distance == null ? 'FEED UNREADABLE' : 'RANGE FINDER'}</Mono>
          </div>
          <div style={{height: 1, background: C.amberLine, margin: '12px 0 8px 0', transformOrigin: 'left', transform: `scaleX(${prog(t, 0.05, 0.4, ease.out3)})`}} />

          <div style={{display: 'flex', alignItems: 'flex-end', gap: 22, minHeight: numSize * 0.9}}>
            {shown != null ? (
              <div style={{display: 'flex', alignItems: 'baseline', transform: `scale(${numScale})`, transformOrigin: 'left bottom'}}>
                <Digits text={shown} size={numSize} cell={0.46} color={t >= hit ? C.white : C.amber} />
                <span style={{fontFamily: F.display, fontSize: numSize * 0.42, color: accent, marginLeft: 8, letterSpacing: '0.04em'}}>M</span>
              </div>
            ) : (
              <div style={{fontFamily: F.display, fontSize: 96, lineHeight: 1, color: C.white, letterSpacing: '0.02em', whiteSpace: 'nowrap'}}>{headline}</div>
            )}
            <div style={{display: 'flex', flexDirection: 'column', gap: 10, paddingBottom: 14}}>
              {tags.map((tag, i) => {
                const p = prog(t, hit + 0.04 + i * 0.08, hit + 0.22 + i * 0.08, ease.back);
                const col = CHIP[tag] || C.amber;
                return (
                  <div
                    key={tag}
                    style={{
                      alignSelf: 'flex-start',
                      fontFamily: F.mono,
                      fontSize: 22,
                      letterSpacing: '0.16em',
                      padding: '3px 12px',
                      color: col === C.amber ? C.ink : '#fff',
                      background: col,
                      transform: `scale(${p})`,
                      transformOrigin: 'left center',
                      opacity: p,
                    }}
                  >
                    {tag}
                  </div>
                );
              })}
            </div>
          </div>

          {shown != null && <Ruler value={shown} max={distance > 300 ? 500 : 300} width={W - 70} accent={accent} locked={t >= hit} p={inP} />}

          <div style={{display: 'flex', alignItems: 'center', gap: 16, marginTop: 14, minHeight: 40}}>
            {icon && <img src={icon} style={{height: 38, maxWidth: 220, objectFit: 'contain', opacity: prog(t, hit - 0.05, hit + 0.2, ease.out2)}} />}
            <div style={{fontFamily: F.mono, fontSize: 30, letterSpacing: '0.1em', color: C.white, opacity: prog(t, hit, hit + 0.25, ease.out2)}}>
              {distance == null ? '' : headline !== 'KILL CONFIRMED' ? headline : ''}
              {victim ? (distance == null || headline === 'KILL CONFIRMED' ? '' : '  ') + '> ' + victim.toUpperCase() : ''}
            </div>
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};
