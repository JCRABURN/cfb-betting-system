// Extract text items (with positions) and, for each Pts digit, the border darkness of the
// left/right buttons on its row. Usage: node extract.js "<pdf>" <out.json>
const fs = require('fs');
const canvasLib = require('@napi-rs/canvas');
globalThis.DOMMatrix = canvasLib.DOMMatrix;
globalThis.Path2D = canvasLib.Path2D;
globalThis.ImageData = canvasLib.ImageData;
const pdfjs = require('pdfjs-dist/legacy/build/pdf.js');

class NodeCanvasFactory {
  create(w, h) { const canvas = canvasLib.createCanvas(w, h); return { canvas, context: canvas.getContext('2d') }; }
  reset(cc, w, h) { cc.canvas.width = w; cc.canvas.height = h; }
  destroy(cc) { cc.canvas.width = 0; cc.canvas.height = 0; }
}

const SCALE = 2;
// Column geometry in PDF points (letter page, 612 wide), measured from the rendered layout:
const AWAY_X = [36, 180], HOME_X = [183, 327], PTS_X = [330, 355];

function minLum(ctx, xPx, yPx, halfW = 2, halfH = 7) {
  const x0 = Math.max(0, Math.floor(xPx - halfW)), y0 = Math.max(0, Math.floor(yPx - halfH));
  const w = halfW * 2 + 1, h = halfH * 2 + 1;
  const d = ctx.getImageData(x0, y0, w, h).data;
  let m = 255;
  for (let i = 0; i < d.length; i += 4) { const l = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; if (l < m) m = l; }
  return Math.round(m);
}

(async () => {
  const [pdfPath, outPath] = process.argv.slice(2);
  const data = new Uint8Array(fs.readFileSync(pdfPath));
  const factory = new NodeCanvasFactory();
  const doc = await pdfjs.getDocument({ data, canvasFactory: factory, useSystemFonts: true, disableFontFace: true }).promise;
  const out = { pages: [] };
  for (let p = 1; p <= doc.numPages; p++) {
    const page = await doc.getPage(p);
    const vp = page.getViewport({ scale: SCALE });
    const cc = factory.create(Math.ceil(vp.width), Math.ceil(vp.height));
    await page.render({ canvasContext: cc.context, viewport: vp, canvasFactory: factory }).promise;
    const tc = await page.getTextContent();
    const pageH = page.getViewport({ scale: 1 }).height;
    const items = tc.items.filter(i => i.str && i.str.trim()).map(i => ({
      s: i.str.trim(), x: +i.transform[4].toFixed(1), y: +(pageH - i.transform[5]).toFixed(1), w: +(i.width || 0).toFixed(1),
    }));
    // Pts digits: single digit 1-5 inside the Pts column
    const digits = items.filter(i => /^[1-5]$/.test(i.s) && i.x >= PTS_X[0] - 4 && i.x <= PTS_X[1]);
    const picks = digits.map(d => {
      const yc = d.y - 3;                     // text baseline -> approx button centre
      const L = minLum(cc.context, AWAY_X[0] * SCALE, yc * SCALE);
      const R = minLum(cc.context, HOME_X[0] * SCALE, yc * SCALE);
      return { digit: +d.s, x: d.x, y: d.y, leftDark: L, rightDark: R };
    });
    out.pages.push({ page: p, height: pageH, items, picks });
  }
  fs.writeFileSync(outPath, JSON.stringify(out));
  const n = out.pages.reduce((a, pg) => a + pg.picks.length, 0);
  console.log(`${pdfPath}: ${doc.numPages} pages, ${n} point digits found`);
})().catch(e => { console.error('ERR', e); process.exit(1); });
