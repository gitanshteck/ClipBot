import React from 'react';
import {AbsoluteFill} from 'remotion';
import {C, F, ease, prog, useClock, useFonts} from '../theme.js';

// The smallest composition there is: a block that slides in and stays, and a word. Used by the tests, which
// need a real render that is fast and whose pixels are known (a solid amber bar at a known place on `hit`).
export const Probe = (props) => {
  useFonts();
  const {t, hit} = useClock(props);
  const {text = 'PROBE'} = props;
  const p = prog(t, 0, hit, ease.out3);
  return (
    <AbsoluteFill>
      <div style={{position: 'absolute', left: 100, top: 100, width: 600 * p, height: 120, background: C.amber}} />
      <div style={{position: 'absolute', left: 100, top: 260, fontFamily: F.display, fontSize: 120, color: C.white, opacity: p}}>{text}</div>
    </AbsoluteFill>
  );
};
