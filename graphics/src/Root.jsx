import React from 'react';
import {Composition} from 'remotion';
import {Acquire} from './comps/Acquire.jsx';
import {Contact} from './comps/Contact.jsx';
import {OpTitle} from './comps/OpTitle.jsx';
import {Probe} from './comps/Probe.jsx';
import {Stats} from './comps/Stats.jsx';
import {Title} from './comps/Title.jsx';

// Every composition is 1920x1080 and is told its own length, hit time and frame rate through props (the
// Python side, clipbot/editmotion.py, owns timing: COMPS there holds the defaults and must list exactly the
// ids registered below - tests/test_editmotion.py checks it).
const meta = ({props}) => ({
  durationInFrames: Math.max(1, Math.round((props.dur ?? 2) * (props.fps ?? 60))),
  fps: props.fps ?? 60,
});

// No demo props here on purpose: Remotion merges `defaultProps` under a render's props, so a default victim or
// distance would show up on a real kill that had none (a plate said KHANNY on four kills before this was removed).
const def = (id, component, dur, hit) => (
  <Composition
    key={id}
    id={id}
    component={component}
    width={1920}
    height={1080}
    fps={60}
    durationInFrames={Math.round(dur * 60)}
    defaultProps={{dur, hit, fps: 60}}
    calculateMetadata={meta}
  />
);

export const Root = () => (
  <>
    {def('Title', Title, 3.6, 1.0)}
    {def('OpTitle', OpTitle, 2.6, 0.5)}
    {def('Contact', Contact, 3.2, 0.55)}
    {def('Stats', Stats, 7.0, 0.6)}
    {def('Acquire', Acquire, 6.0, 0.8)}
    {def('Probe', Probe, 1.0, 0.5)}
  </>
);
