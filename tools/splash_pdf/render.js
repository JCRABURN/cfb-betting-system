// Render PDF pages to PNG. Usage: node render.js "<pdf path>" <outPrefix> [scale]
const fs = require('fs');
const path = require('path');
const canvasLib = require('@napi-rs/canvas');
globalThis.DOMMatrix = canvasLib.DOMMatrix;
globalThis.Path2D = canvasLib.Path2D;
globalThis.ImageData = canvasLib.ImageData;

const pdfjs = require('pdfjs-dist/legacy/build/pdf.js');

class NodeCanvasFactory {
  create(width, height) {
    const canvas = canvasLib.createCanvas(width, height);
    return { canvas, context: canvas.getContext('2d') };
  }
  reset(cc, width, height) { cc.canvas.width = width; cc.canvas.height = height; }
  destroy(cc) { cc.canvas.width = 0; cc.canvas.height = 0; }
}

(async () => {
  const [pdfPath, outPrefix, scaleArg] = process.argv.slice(2);
  const scale = parseFloat(scaleArg || '1.6');
  const data = new Uint8Array(fs.readFileSync(pdfPath));
  const factory = new NodeCanvasFactory();
  const doc = await pdfjs.getDocument({ data, canvasFactory: factory, useSystemFonts: true, disableFontFace: true }).promise;
  console.log('pages:', doc.numPages);
  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i);
    const viewport = page.getViewport({ scale });
    const cc = factory.create(Math.ceil(viewport.width), Math.ceil(viewport.height));
    await page.render({ canvasContext: cc.context, viewport, canvasFactory: factory }).promise;
    const out = `${outPrefix}_p${i}.png`;
    fs.writeFileSync(out, cc.canvas.toBuffer('image/png'));
    console.log('wrote', out, Math.ceil(viewport.width) + 'x' + Math.ceil(viewport.height));
  }
})().catch(e => { console.error('ERR', e); process.exit(1); });
