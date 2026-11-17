// The look of AFTER ACTION: a tactical debrief. Graphite, one signal colour (amber), alert red for a kill,
// condensed display type for the words and a mono face for the data. Everything is timed in seconds
// against the composition's own `dur` and `hit` props (see useClock), never in frames.
import {useCurrentFrame, useVideoConfig, continueRender, delayRender, staticFile} from 'remotion';
import {useEffect, useState} from 'react';

export const C = {
  ink: '#06080A',
  ink2: '#0C1014',
  panel: 'rgba(7,10,13,0.80)',
  amber: '#FFB300',
  amberSoft: 'rgba(255,179,0,0.16)',
  amberLine: 'rgba(255,179,0,0.55)',
  red: '#FF3B2F',
  white: '#F4F1E8',
  dim: '#8C939B',
  grid: 'rgba(255,255,255,0.045)',
};

// The look of TECK HIGHLIGHTS (the `hl` look): a gaming-channel reel rather than a debrief. Near-black ink, the
// channel's cyan (his facecam border), white type, a hot red for a headshot and gold for the game's own medals.
export const H = {
  ink: '#06080C',
  panel: 'rgba(7,10,15,0.88)',
  cyan: '#35D0FF',
  cyanSoft: 'rgba(53,208,255,0.18)',
  cyanLine: 'rgba(53,208,255,0.6)',
  white: '#F6F8FB',
  dim: '#9FB0BF',
  hot: '#FF4D6D',
  gold: '#FFC93C',
};

export const F = {
  display: '"Bebas Neue", Impact, sans-serif',
  heavy: 'Anton, Impact, sans-serif',
  mono: '"Share Tech Mono", Consolas, monospace',
};

export const clamp01 = (x) => Math.min(1, Math.max(0, x));
export const lerp = (a, b, t) => a + (b - a) * t;

export const ease = {
  linear: (t) => t,
  out2: (t) => 1 - (1 - t) * (1 - t),
  out3: (t) => 1 - Math.pow(1 - t, 3),
  out5: (t) => 1 - Math.pow(1 - t, 5),
  in2: (t) => t * t,
  in3: (t) => t * t * t,
  inOut3: (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  back: (t) => {
    const c1 = 1.70158;
    const c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
  },
};

// progress of `t` between `a` and `b` seconds, eased, clamped to 0..1
export const prog = (t, a, b, fn = ease.out3) => fn(clamp01((t - a) / Math.max(1e-6, b - a)));

// deterministic pseudo-random in 0..1 from an integer seed (a render must be repeatable)
export const rand = (n) => {
  const s = Math.sin(n * 127.1 + 311.7) * 43758.5453123;
  return s - Math.floor(s);
};

export const useClock = (props) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const t = frame / fps;
  return {t, fps, frame, dur: durationInFrames / fps, hit: props.hit ?? 0.5};
};

const FACES = [
  ['Bebas Neue', 'fonts/BebasNeue-Regular.ttf'],
  ['Anton', 'fonts/Anton-Regular.ttf'],
  ['Share Tech Mono', 'fonts/ShareTechMono-Regular.woff2'],
];

let loading = null;
const loadAll = () => {
  if (!loading) {
    loading = Promise.all(
      FACES.map(([name, file]) => {
        const face = new FontFace(name, `url(${staticFile(file)})`);
        return face.load().then((f) => document.fonts.add(f));
      })
    );
  }
  return loading;
};

// holds the render until the fonts are in; without it the first frames come out in a fallback face
export const useFonts = () => {
  const [handle] = useState(() => delayRender('fonts'));
  useEffect(() => {
    loadAll().then(
      () => continueRender(handle),
      (e) => {
        console.error('font load failed', e);
        continueRender(handle);
      }
    );
  }, [handle]);
};

export const pad2 = (n) => String(Math.max(0, Math.round(n))).padStart(2, '0');
