const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");
const code = fs.readFileSync(path.join(__dirname, "../console/graph.js"), "utf8");
const anchor = "window.GraphExplorer = GraphExplorer;";
assert.equal(code.split(anchor).length, 2);
const sandbox = vm.createContext({
  window: {}, S: { repos: [], groups: [], servers: [] }, I18N: { ko: {}, en: {} }, LANG: "en",
  t: x => x, serverName: x => x, console, URL, URLSearchParams, TextDecoder, TextEncoder,
  setTimeout, clearTimeout, AbortController, DOMException, Blob,
  document: { getElementById: () => null },
});
new vm.Script(code.replace(anchor, `window.labelTest = {fitNodeLabel, drawBoundedNodeLabel, sigmaSettings, GX}; ${anchor}`)).runInContext(sandbox);
const { fitNodeLabel, drawBoundedNodeLabel, sigmaSettings, GX } = sandbox.window.labelTest;
const settings = { labelSize: 12, labelWeight: "600", labelFont: "sans-serif", labelColor: { color: "#0F172A" } };
function context() {
  const calls = [];
  return {
    calls, canvas: { width: 1280, height: 800, clientWidth: 640, clientHeight: 400 },
    measureText: text => ({ width: Array.from(text).length * 6, actualBoundingBoxAscent: 9, actualBoundingBoxDescent: 3 }),
    save() { calls.push(["save"]); }, restore() { calls.push(["restore"]); },
    beginPath() {}, arc() {}, fill() {},
    fillRect(...args) { calls.push(["box", ...args]); },
    fillText(...args) { calls.push(["text", ...args]); },
    strokeText(...args) { calls.push(["halo", ...args]); },
  };
}
function fits(label, bounds) {
  assert.ok(label);
  assert.ok(label.x >= 8 && label.x + label.width <= bounds.width - 8);
  assert.ok(label.y - label.ascent >= 8 && label.y + label.descent <= bounds.height - 8);
}
for (const [name, x, y] of [["right", 630, 200], ["left", 3, 200], ["top", 320, 0], ["bottom", 320, 400]]) {
  test(`${name}-edge labels remain inside the CSS viewport`, () => {
    const ctx = context(), data = { label: "demo-pension-backend", x, y, size: 15 };
    const label = fitNodeLabel(ctx, data, settings, { width: 640, height: 400 });
    fits(label, { width: 640, height: 400 });
    assert.equal(label.label, data.label);
    if (name === "right") assert.equal(label.side, "left");
  });
}
test("oversized labels ellipsize at grapheme boundaries and retain original data", () => {
  const glyph = "👩🏽‍💻", original = glyph.repeat(100);
  const data = { label: original, x: 50, y: 50, size: 10 };
  const label = fitNodeLabel(context(), data, settings, { width: 180, height: 100 });
  fits(label, { width: 180, height: 100 });
  assert.equal(label.truncated, true);
  assert.ok(label.label.endsWith("…"));
  assert.equal(label.label.slice(0, -1).split(glyph).join(""), "");
  assert.equal(data.label, original);
});
test("tiny/invalid canvases and invalid coordinates do not draw corrupt labels", () => {
  const data = { label: "label", x: 30, y: 30, size: 10 };
  for (const bounds of [{ width: 0, height: 0 }, { width: 30, height: 100 }, { width: 100, height: 20 }]) {
    assert.equal(fitNodeLabel(context(), data, settings, bounds), null);
  }
  for (const patch of [{ x: NaN }, { y: Infinity }, { size: Infinity }, { size: -2 }, { label: null }]) {
    assert.equal(fitNodeLabel(context(), { ...data, ...patch }, settings, { width: 640, height: 400 }), null);
  }
});
test("labels without side room move below or above their node", () => {
  for (const y of [100, 195]) {
    const label = fitNodeLabel(context(), { label: "A".repeat(30), x: 110, y, size: 20 }, settings, { width: 220, height: 220 });
    fits(label, { width: 220, height: 220 });
    assert.equal(label.side, "center");
    assert.ok(label.y - label.ascent >= y + 25 || label.y + label.descent <= y - 25);
  }
});
test("high-DPI drawing uses renderer CSS dimensions rather than canvas pixel dimensions", () => {
  const ctx = context();
  GX.renderer = { getDimensions: () => ({ width: 640, height: 400 }) };
  drawBoundedNodeLabel(ctx, { label: "backend", x: 630, y: 200, size: 15 }, settings);
  const call = ctx.calls.find(call => call[0] === "text");
  assert.ok(call && call[2] + ctx.measureText(call[1]).width <= 632);
  assert.equal(ctx.calls[0][0], "save");
  assert.equal(ctx.calls.at(-1)[0], "restore");
});
test("selected and hovered label backgrounds remain inside the viewport", () => {
  const ctx = context();
  GX.renderer = { getDimensions: () => ({ width: 640, height: 400 }) };
  drawBoundedNodeLabel(ctx, { label: "backend", x: 630, y: 398, size: 15 }, settings, true);
  const box = ctx.calls.find(call => call[0] === "box");
  assert.ok(box);
  const [, x, y, w, h] = box;
  assert.ok(x >= 0 && y >= 0 && x + w <= 640 && y + h <= 400);
  assert.ok(ctx.calls.some(call => call[0] === "text"));
});
test("Sigma uses the bounded renderer for both ordinary and hovered labels", () => {
  const config = sigmaSettings(4, 6);
  assert.equal(config.defaultDrawNodeLabel, drawBoundedNodeLabel);
  assert.equal(typeof config.defaultDrawNodeHover, "function");
});
