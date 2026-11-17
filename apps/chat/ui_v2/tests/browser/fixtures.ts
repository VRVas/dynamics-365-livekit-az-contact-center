import { test as base, expect, type Locator, type Page, type TestInfo } from '@playwright/test';

export const test = base.extend({
  page: async ({ page }, run) => {
    await page.route('**/*', (route) => {
      const url = new URL(route.request().url());
      if (url.hostname !== '127.0.0.1' || /^\/(backend|voice-api|agent-backend)(\/|$)/.test(url.pathname)) {
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    await run(page);
  },
});

export { expect };

/** Waits until the control and every ancestor have finished fading in. */
export async function expectOpaque(control: Locator, minimum = 0.99) {
  await expect(control).toBeVisible();
  await expect.poll(() => control.evaluate((element) => {
    let opacity = 1;
    for (let node: Element | null = element; node; node = node.parentElement) {
      opacity *= Number(getComputedStyle(node).opacity);
    }
    return opacity;
  }), { message: 'cumulative opacity' }).toBeGreaterThanOrEqual(minimum);
}

async function settle(control: Locator) {
  await control.page().evaluate(async () => { await document.fonts.ready; });
  await expectOpaque(control);
}

/** Lists what cuts the control off (the viewport or a clipping ancestor) and which containers are scrolled. */
function inspect(control: Locator) {
  return control.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const describe = (node: Element) => [node.tagName.toLowerCase(), ...String(node.className).split(/\s+/).filter(Boolean).slice(0, 4)].join('.');
    const areas = [{
      name: 'viewport', left: 0, top: 0,
      right: document.documentElement.clientWidth, bottom: document.documentElement.clientHeight,
    }];
    const scrolled = window.scrollX || window.scrollY ? ['window'] : [];
    for (let node = element.parentElement; node && node !== document.body; node = node.parentElement) {
      if (node.scrollTop || node.scrollLeft) scrolled.push(describe(node));
      const style = getComputedStyle(node);
      if (style.overflowX === 'visible' && style.overflowY === 'visible') continue;
      const box = node.getBoundingClientRect();
      const left = box.left + node.clientLeft;
      const top = box.top + node.clientTop;
      areas.push({ name: describe(node), left, top, right: left + node.clientWidth, bottom: top + node.clientHeight });
    }
    const tolerance = 0.5;
    const round = (values: number[]) => values.map((value) => Math.round(value));
    const clipped = areas
      .filter((area) => rect.left < area.left - tolerance || rect.top < area.top - tolerance
        || rect.right > area.right + tolerance || rect.bottom > area.bottom + tolerance)
      .map((area) => ({
        clippedBy: area.name,
        control: round([rect.left, rect.top, rect.right, rect.bottom]),
        visibleArea: round([area.left, area.top, area.right, area.bottom]),
      }));
    return { clipped, scrolled };
  });
}

/** Scrolls the control into view, then fails if the viewport or any clipping ancestor cuts it off. */
export async function expectFullyVisible(control: Locator, label: string) {
  await settle(control);
  await control.scrollIntoViewIfNeeded();
  expect((await inspect(control)).clipped, `${label} must be fully visible`).toEqual([]);
}

/** Fails unless every control is fully visible at the same time, with no page or container scrolled. */
export async function expectVisibleTogether(controls: Locator[], label: string) {
  const findings = [];
  for (const control of controls) {
    await settle(control);
    findings.push(await inspect(control));
  }
  expect(findings, `${label} must be fully visible together without scrolling`)
    .toEqual(controls.map(() => ({ clipped: [], scrolled: [] })));
}

/** Saves a settled screenshot as a test attachment; never captures a fading control. */
export async function captureEvidence(page: Page, testInfo: TestInfo, name: string, controls: Locator[]) {
  for (const control of controls) await settle(control);
  const path = testInfo.outputPath(`${name}.png`);
  await page.screenshot({ path, caret: 'hide' });
  await testInfo.attach(name, { path, contentType: 'image/png' });
}
