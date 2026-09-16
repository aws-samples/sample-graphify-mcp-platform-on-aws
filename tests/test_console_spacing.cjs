/* Geometric regression checks for action placement and readable form text.
 * Requires an existing Playwright package through GRAPHIFY_PLAYWRIGHT_MODULE.
 * GRAPHIFY_SPACING_SNAPSHOT may point to a copied console/tests baseline.
 * GRAPHIFY_SPACING_BASELINE=1 records violations without asserting a pass.
 */
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const crypto = require("node:crypto");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.GRAPHIFY_PLAYWRIGHT_MODULE || "playwright");
const ROOT = path.resolve(process.env.GRAPHIFY_SPACING_SNAPSHOT || path.join(__dirname, ".."));
const OUT = process.env.GRAPHIFY_SPACING_OUT || "/tmp/graphify-console-spacing";
const baseline = process.env.GRAPHIFY_SPACING_BASELINE === "1";
const filename = path.join(ROOT, "tests/test_console_ux.cjs");
const mod = new Module(filename);
mod.filename = filename; mod.paths = Module._nodeModulePaths(path.dirname(filename));
mod._compile(fs.readFileSync(filename, "utf8") + "\nmodule.exports={uxHarness,prepareAssets,panelDialog,assertPanelOpen,closePanelModal};", filename);
const { uxHarness, prepareAssets, panelDialog, assertPanelOpen, closePanelModal } = mod.exports;
const { group, assetHashes } = require(path.join(ROOT, "tests/test_console_group_navigation.cjs"));
const GROUP = "grp_a123456789abcdef0123456789abcdef";
const EXPECTED_MEASUREMENTS = 88; // 11 actions × 4 viewport widths × 2 languages.
const results = [], violations = [], screenshots = [];

async function measure(page, name, selector, options = {}) {
  const button = page.locator(selector);
  await button.scrollIntoViewIfNeeded();
  await button.click({ trial: true });
  const facts = await button.evaluate((button, opts) => {
    const dialog = button.closest("dialog.console-panel-dialog");
    // The original section now lives inside the scrolling modal body. Its
    // actual content width already excludes that body's scrollbar gutter.
    const panel = dialog ? document.getElementById(dialog.dataset.panelId) : button.closest("section");
    const b = button.getBoundingClientRect(), p = panel.getBoundingClientRect(), style = getComputedStyle(panel);
    const right = p.left + panel.clientLeft + panel.clientWidth - parseFloat(style.paddingRight);
    const actions = button.parentElement, a = actions.getBoundingClientRect();
    const body = button.closest(".row");
    const controls = body ? [...body.querySelectorAll("input:not([type=hidden]),textarea,.combo-input")].filter(el => el.getClientRects().length) : [];
    const previousBottom = controls.length ? Math.max(...controls.map(el => el.getBoundingClientRect().bottom)) : null;
    const modalBody = dialog?.querySelector(".console-panel__body"), m = modalBody?.getBoundingClientRect();
    return {
      width: innerWidth, height: innerHeight,
      button: { x: b.x, y: b.y, right: b.right, bottom: b.bottom, width: b.width, height: b.height },
      contentRight: right, rightGap: right - b.right, actionWidth: a.width,
      footerGap: previousBottom == null ? null : b.top - previousBottom,
      clipped: button.scrollWidth > button.clientWidth + 1 || button.scrollHeight > button.clientHeight + 1,
      rootWidth: document.documentElement.scrollWidth,
      panelId: dialog?.dataset.panelId || null,
      modal: modalBody ? {
        open: dialog.open && dialog.matches(":modal"),
        left: m.left, right: m.right, top: m.top, bottom: m.bottom,
        width: modalBody.clientWidth, scrollWidth: modalBody.scrollWidth,
      } : null,
      footer: opts.footer,
    };
  }, options);
  results.push({ name, ...facts });
  if (Math.abs(facts.rightGap) > 3) violations.push({ name, problem: "primary action is not right-aligned", ...facts });
  if (facts.button.width > 260 || facts.button.width < 70) violations.push({ name, problem: "action has inappropriate width", ...facts });
  if (facts.clipped) violations.push({ name, problem: "button text is clipped", ...facts });
  if (facts.rootWidth > facts.width + 1) violations.push({ name, problem: "viewport overflow", ...facts });
  if (facts.button.height <= 0 || facts.button.x < -1 || facts.button.right > facts.width + 1
    || facts.button.y < -1 || facts.button.bottom > facts.height + 1) {
    violations.push({ name, problem: "primary action is not fully reachable in the viewport", ...facts });
  }
  if (facts.modal && (!facts.modal.open || facts.modal.scrollWidth > facts.modal.width + 1
    || facts.button.x < facts.modal.left - 1 || facts.button.right > facts.modal.right + 1
    || facts.button.y < facts.modal.top - 1 || facts.button.bottom > facts.modal.bottom + 1)) {
    violations.push({ name, problem: "primary action overflows or is clipped by its modal body", ...facts });
  }
  if (options.footer && facts.footerGap != null && facts.footerGap < 12) violations.push({ name, problem: "insufficient footer clearance", ...facts });
}
async function inspectText(page, scope, context) {
  // Mounting moves headings out of their original section into the header.
  const bad = await page.locator(scope).evaluate(root => [...(root.closest("dialog.console-panel-dialog") || root).querySelectorAll("h1,h2,h3,label,button")]
    .filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== "hidden")
    .filter(el => el.scrollWidth > el.clientWidth + 2 || el.scrollHeight > el.clientHeight + 2)
    .map(el => ({ tag: el.tagName, id: el.id, text: el.textContent.slice(0, 160), width: el.clientWidth, scroll: el.scrollWidth })));
  for (const item of bad) violations.push({ context, problem: "heading/label/button content is clipped", ...item });
}
async function capture(page, name) {
  const dialog = page.locator("dialog[open]").last();
  if (await dialog.count()) await dialog.screenshot({ path: path.join(OUT, name + ".png"), animations: "disabled" });
  else await page.screenshot({ path: path.join(OUT, name + ".png"), fullPage: true, animations: "disabled" });
  screenshots.push(name + ".png");
}
async function navigate(page, tab) {
  const active = page.locator("dialog.console-panel-dialog[open]");
  if (await active.count()) {
    assert.equal(await active.count(), 1, "Only one task panel may be open before navigation");
    await closePanelModal(page, await active.getAttribute("data-panel-id"));
  }
  assert.equal(await page.locator("dialog[open]").count(), 0, "Close visible modals before using background navigation");
  await page.locator(`nav.tabs [data-tab="${tab}"]`).click();
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  await prepareAssets();
  const assets = assetHashes();
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  let h;
  try {
    h = await uxHarness(browser, true);
    const page = h.page;
    const members = h.data.repos.slice(0, 2).map((r, i) => ({ source_id: r.repo_id, role: i ? "backend" : "frontend", description: "Layout review source" }));
    h.data.groups = [group(GROUP, { name: "Button and spacing review", sources: members,
      active_source_versions: Object.fromEntries(h.data.repos.slice(0, 2).map(r => [r.repo_id, r.active_source_version])) })];
    await page.evaluate(() => refreshAll());
    for (const lang of ["ko", "en"]) {
      await page.evaluate(lang => setLang(lang), lang);
      for (const width of [1440, 1024, 768, 390]) {
        await page.setViewportSize({ width, height: 1000 });
        await navigate(page, "play");
        await measure(page, `${lang}-${width}-run`, "#btn-direct-run", { footer: true });
        await measure(page, `${lang}-${width}-load-tools`, "#btn-play-load", { footer: true });
        await measure(page, `${lang}-${width}-send`, "#btn-chat-send");
        const labels = await page.evaluate(() => {
          const a = document.getElementById("direct-tool").closest(".row > div").querySelector("label").getBoundingClientRect();
          const b = document.getElementById("direct-args").closest(".row > div").querySelector("label").getBoundingClientRect();
          return { sameRow: Math.abs(a.left - b.left) > 20, toolTop: a.top, jsonTop: b.top };
        });
        if (labels.sameRow && Math.abs(labels.toolTop - labels.jsonTop) > 2) violations.push({ lang, width, problem: "Tool and JSON labels are not top-aligned", ...labels });
        await inspectText(page, "#page-play", `${lang}-${width}-play`);
        if (lang === "ko") {
          await capture(page, `${width}-play`);
          await page.locator("section").filter({ has: page.locator("#btn-direct-run") })
            .screenshot({ path: path.join(OUT, `${width}-direct.png`), animations: "disabled" });
          screenshots.push(`${width}-direct.png`);
        }

        await navigate(page, "keys");
        assert.equal(await page.locator("#key-form").isVisible(), false, "Key page defaults to its list");
        await page.locator("#btn-key-open").click();
        await assertPanelOpen(page, "key-form");
        await measure(page, `${lang}-${width}-key`, "#btn-key-create", { footer: true });
        await inspectText(page, "#page-keys", `${lang}-${width}-keys`);
        await inspectText(page, "#key-form", `${lang}-${width}-key-modal`);
        await navigate(page, "admin");
        assert.equal(await page.locator("#invite-form").isVisible(), false, "Admin page defaults to its list");
        await page.locator("#btn-invite-open").click();
        await assertPanelOpen(page, "invite-form");
        await measure(page, `${lang}-${width}-invite`, "#btn-invite", { footer: true });
        await inspectText(page, "#page-admin", `${lang}-${width}-admin`);
        await inspectText(page, "#invite-form", `${lang}-${width}-invite-modal`);
        if (lang === "ko") await capture(page, `${width}-admin`);
        for (const [tab, refresh] of [["keys", "btn-keys-refresh"], ["admin", "btn-users-refresh"]]) {
          await navigate(page, tab);
          const aligned = await page.locator(`#${refresh}`).evaluate(el => !!el.closest(".page-actions") && !el.closest("h2"));
          if (!aligned) violations.push({ lang, width, tab, problem: "refresh action is embedded in the title instead of the action toolbar" });
        }

        await navigate(page, "repos");
        await page.locator("#btn-register-open").click();
        await assertPanelOpen(page, "registration-panel");
        for (const type of ["git", "url", "files"]) {
          await page.locator(`#reg-source-seg [data-src="${type}"]`).click();
          await measure(page, `${lang}-${width}-register-${type}`, `#reg-${type}-fields .reg-submit`, { footer: true });
        }
        await inspectText(page, "#registration-panel", `${lang}-${width}-registration`);
        await closePanelModal(page, "registration-panel");
        await page.evaluate(() => openLlmPanel(S.repos.find(r => r.source_type === "files" && r.manageable)));
        await assertPanelOpen(page, "llm-panel");
        await measure(page, `${lang}-${width}-llm-save`, "#lp-save", { footer: true });
        await inspectText(page, "#llm-panel", `${lang}-${width}-llm`);
        await closePanelModal(page, "llm-panel");
        await page.evaluate(() => openMembersPanel(S.repos.find(r => r.owned && r.graph_scope === "private").repo_id));
        await assertPanelOpen(page, "members-panel");
        await measure(page, `${lang}-${width}-member-add`, "#mp-add", { footer: true });
        await closePanelModal(page, "members-panel");

        await navigate(page, "groups");
        await page.evaluate(gid => SourceGroups.manage(gid, "edit"), GROUP);
        await assertPanelOpen(page, "sg-editor");
        await measure(page, `${lang}-${width}-group-save`, "#sg-save");
        const order = await page.locator("#sg-save").evaluate(el => el === el.parentElement.lastElementChild);
        if (!order) violations.push({ lang, width, problem: "group primary save does not follow cancel" });
        await inspectText(page, "#sg-editor", `${lang}-${width}-group-editor`);
        if (lang === "ko") await capture(page, `${width}-group-editor`);
        await page.locator('#sg-form [data-i18n="sg.cancel"]').click();
        await panelDialog(page, "sg-editor").waitFor({ state: "hidden" });
      }
    }
    h.assertClean();
    assert.equal(results.length, EXPECTED_MEASUREMENTS, "Keep all language/viewport/action measurements");
    for (const asset of assets) asset.changedDuringRun =
      crypto.createHash("sha256").update(fs.readFileSync(path.join(ROOT, asset.file))).digest("hex") !== asset.sha256;
    const report = { baseline, passed: violations.length === 0 && assets.every(a => !a.changedDuringRun),
      assets, expectedMeasurements: EXPECTED_MEASUREMENTS, measurements: results.length, results, violations, screenshots };
    fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ baseline, measurements: results.length, violations: violations.length, out: OUT }));
    if (!baseline) assert.deepEqual(violations, [], "Unexpected alignment/clipping problems; inspect results.json");
    assert.ok(assets.every(a => !a.changedDuringRun), "Assets changed during geometry checks; rerun on final files");
  } finally {
    if (h) await h.context.close();
    await browser.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
