// Batch renderer for the edit engine (clipbot/editmotion.py). Usage: node render.mjs <batch.json>
//
// batch = {chrome: path|null, jobs: [{key, comp, props, dur, hit, fps, scale, out}], stills: [{comp, props, frame, dur, hit, fps, scale, out}]}
//
// One process draws every job so the bundle is built once and one browser is reused. A job is
// written as <out>.part.mov and renamed when whole, so an interrupted run leaves nothing that
// looks finished. One JSON line per finished job on stdout (the Python side reads them).
// Props whose value is "file:<path>" are replaced by a data: URI of that file, because the page
// is served over http and cannot load a file: URL.
import {bundle} from '@remotion/bundler';
import {openBrowser, renderMedia, renderStill, selectComposition} from '@remotion/renderer';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const batch = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const say = (o) => console.log(JSON.stringify(o));

const MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.svg': 'image/svg+xml'};

const inline = (v) => {
  if (typeof v === 'string' && v.startsWith('file:')) {
    const p = v.slice(5);
    if (!fs.existsSync(p)) {
      console.error(`missing file in props: ${p}`);
      return null;                       // the composition draws without it (an icon the feed never gave)
    }
    const mime = MIME[path.extname(p).toLowerCase()] || 'application/octet-stream';
    return `data:${mime};base64,${fs.readFileSync(p).toString('base64')}`;
  }
  if (Array.isArray(v)) return v.map(inline);
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, inline(x)]));
  return v;
};

const chromeOpts = batch.chrome ? {browserExecutable: batch.chrome, chromeMode: 'chrome-for-testing'} : {};

try {
  const serveUrl = await bundle({entryPoint: path.join(here, 'src', 'index.js')});
  const browser = await openBrowser('chrome', {...chromeOpts, logLevel: 'error'});
  try {
    for (const job of batch.jobs || []) {
      const inputProps = {...inline(job.props || {}), dur: job.dur, hit: job.hit, fps: job.fps};
      const composition = await selectComposition({serveUrl, id: job.comp, inputProps, puppeteerInstance: browser, ...chromeOpts});
      const part = job.out.replace(/\.mov$/, '.part.mov');
      await renderMedia({
        composition, serveUrl, inputProps, puppeteerInstance: browser, ...chromeOpts,
        codec: 'prores', proResProfile: '4444', pixelFormat: 'yuva444p10le', imageFormat: 'png',
        scale: job.scale || 1, outputLocation: part, overwrite: true, logLevel: 'error',
      });
      fs.renameSync(part, job.out);
      say({event: 'done', comp: job.comp, key: job.key, frames: composition.durationInFrames});
    }
    for (const s of batch.stills || []) {
      const inputProps = {...inline(s.props || {}), dur: s.dur, hit: s.hit, fps: s.fps};
      const composition = await selectComposition({serveUrl, id: s.comp, inputProps, puppeteerInstance: browser, ...chromeOpts});
      await renderStill({
        composition, serveUrl, inputProps, puppeteerInstance: browser, ...chromeOpts,
        frame: s.frame, output: s.out, imageFormat: 'png', scale: s.scale || 1, overwrite: true, logLevel: 'error',
      });
      say({event: 'still', comp: s.comp, out: s.out});
    }
  } finally {
    await browser.close({silent: true});
  }
} catch (err) {
  say({event: 'error', message: String((err && err.stack) || err)});
  process.exit(1);
}
process.exit(0);
