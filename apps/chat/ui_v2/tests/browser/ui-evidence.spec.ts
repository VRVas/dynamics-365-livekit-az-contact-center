import { captureEvidence, expect, expectFullyVisible, expectVisibleTogether, test } from './fixtures';

// Configured copy: voice-first layout with chat, browser voice and phone, Arabic by default.
test.use({
  baseURL: 'http://127.0.0.1:38472',
  // A synthetic microphone; the voice WebSocket below is mocked, so no audio leaves the browser.
  launchOptions: { args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] },
});

const LABELS = {
  ar: { language: 'تغيير اللغة', orb: 'بدء التحدث', activeOrb: 'إيقاف', start: 'بدء', end: 'إنهاء', composer: 'اكتب رسالتك…', send: 'إرسال الرسالة', listening: 'يستمع' },
  en: { language: 'Change language', orb: 'Start talking', activeOrb: 'Stop', start: 'Start', end: 'End', composer: 'Type a message…', send: 'Send message', listening: 'Listening' },
};

test.beforeEach(async ({ page }) => {
  await page.route('**/backend/profiles', (route) => route.fulfill({
    json: [{ id: 'test-record', display_name: 'Backend record', display_name_ar: 'سجل من الخدمة', phone: null }],
  }));
  await page.route('**/backend/conversation/**/stream', (route) => route.fulfill({
    contentType: 'application/x-ndjson',
    body: '["start","assistant"]\n["delta",{"content":"A configured reply."}]\n["end",{}]\n',
  }));
});

for (const viewport of [{ width: 390, height: 844 }, { width: 1440, height: 1000 }]) {
  test(`voice orb and Start button are visible together without scrolling at ${viewport.width}x${viewport.height} in both directions`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/');
    for (const locale of ['ar', 'en'] as const) {
      if (locale === 'en') {
        await page.getByRole('button', { name: LABELS.ar.language, exact: true }).click();
        await page.reload();
      }
      await expect(page.locator('html')).toHaveAttribute('lang', locale);
      const labels = LABELS[locale];
      await expectVisibleTogether([
        page.getByRole('button', { name: labels.orb, exact: true }).locator('canvas'),
        page.getByRole('button', { name: labels.start, exact: true }),
      ], `${locale} voice orb and Start button`);
    }
  });
}

test('mobile evidence captures settled voice and chat states with a mocked voice session', async ({ page }, testInfo) => {
  const voiceMessages: string[] = [];
  await page.routeWebSocket('**/voice-api/realtime', (socket) => {
    socket.onMessage((data) => {
      const message = JSON.parse(String(data));
      voiceMessages.push(message.type);
      if (message.type === 'session.update') socket.send(JSON.stringify({ type: 'session.updated' }));
    });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const labels = LABELS.ar;

  const orb = page.getByRole('button', { name: labels.orb, exact: true }).locator('canvas');
  const start = page.getByRole('button', { name: labels.start, exact: true });
  await expectVisibleTogether([orb, start], 'voice orb and Start button');
  await captureEvidence(page, testInfo, 'mobile-voice', [orb, start]);

  await start.click();
  await expect(page.getByRole('status').filter({ hasText: labels.listening })).toBeVisible();
  const activeOrb = page.getByRole('button', { name: labels.activeOrb, exact: true }).locator('canvas');
  const end = page.getByRole('button', { name: labels.end, exact: true });
  await expectVisibleTogether([activeOrb, end], 'active voice orb and End button');
  await captureEvidence(page, testInfo, 'mobile-voice-listening', [activeOrb, end]);
  await end.click();
  await expect(start).toBeVisible();
  expect(voiceMessages[0]).toBe('session.update');

  const composer = page.getByRole('textbox', { name: labels.composer });
  await composer.fill('أحتاج المساعدة');
  await page.getByRole('button', { name: labels.send, exact: true }).click();
  const reply = page.getByText('A configured reply.', { exact: true });
  await expectFullyVisible(reply, 'chat reply');
  await expectFullyVisible(composer, 'chat composer');
  await captureEvidence(page, testInfo, 'mobile-chat', [reply, composer]);
});
