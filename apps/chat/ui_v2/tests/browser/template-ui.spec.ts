import { expect, test } from './fixtures';

test.beforeEach(async ({ page }) => {
  await page.route('**/backend/conversation/**/stream', (route) => route.fulfill({
    contentType: 'application/x-ndjson',
    body: [
      ['start', 'assistant'],
      ['delta', { content: 'يمكنك متابعة طلبك.\n\n| الطلب | المرجع |\n|---|---|\n| مساعدة | `REF-0123456789ABCDEF` |\n\n[المصدر](https://example.test/help)' }],
      ['end', {}],
    ].map((item) => JSON.stringify(item)).join('\n') + '\n',
  }));
});

test('neutral defaults mount only chat, without profile, phone, voice or background requests', async ({ page }) => {
  const serviceRequests: string[] = [];
  const sockets: string[] = [];
  page.on('request', (request) => {
    if (/\/(backend|voice-api)\//.test(request.url())) serviceRequests.push(request.url());
  });
  page.on('websocket', (socket) => {
    if (!socket.url().includes('/_next/')) sockets.push(socket.url());
  });
  const serverHtml = await (await page.request.get('/')).text();
  expect(serverHtml).toMatch(/<html[^>]*lang="en"[^>]*dir="ltr"/);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Contact center' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'How can we help?' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Type a message…' })).toBeVisible();
  await expect(page.locator('header img')).toHaveCount(0);
  await expect(page.locator('aside')).toHaveCount(0);
  await expect(page.getByLabel('Profile', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Place call' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Start talking' })).toHaveCount(0);
  expect(serviceRequests).toEqual([]);
  expect(sockets).toEqual([]);
});

test('explicit language switching preserves drafts, RTL direction and the neutral header', async ({ page }) => {
  await page.goto('/');
  expect(await page.locator('.brand-header').evaluate((header) => getComputedStyle(header, '::before').backgroundImage)).toContain('to right');
  await page.getByRole('textbox', { name: 'Type a message…' }).fill('Draft remains through switching');
  await page.getByRole('button', { name: 'Change language', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'ar');
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(page.getByRole('heading', { name: 'كيف يمكننا مساعدتك؟' })).toBeVisible();
  expect(await page.locator('.brand-header').evaluate((header) => getComputedStyle(header, '::before').backgroundImage)).toContain('to left');
  await expect(page.getByRole('textbox', { name: 'اكتب رسالتك…' })).toHaveValue('Draft remains through switching');
  await page.getByRole('button', { name: 'تغيير اللغة', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('dir', 'ltr');
  await expect(page.getByRole('textbox', { name: 'Type a message…' })).toHaveValue('Draft remains through switching');
});

test('mixed-language messages and table references persist without unselected handover controls', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Change language', exact: true }).click();
  const composer = page.getByRole('textbox', { name: 'اكتب رسالتك…' });
  await composer.fill('أحتاج المساعدة');
  await expect(composer).toHaveAttribute('dir', 'auto');
  await page.getByRole('button', { name: 'إرسال الرسالة', exact: true }).click();
  await expect(page.getByRole('link', { name: 'المصدر', exact: true })).toHaveAttribute('href', 'https://example.test/help');
  await expect(page.getByRole('button', { name: 'نسخ الرسالة' })).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'التحدث مع موظف', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'تغيير اللغة', exact: true }).click();
  await expect(page.getByText('أحتاج المساعدة', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Copy message' })).toHaveCount(1);
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('dir', 'ltr');
  await expect(page.getByRole('heading', { name: 'How can we help?' })).toBeVisible();
});

test('Arabic remains usable on a narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: 'Change language', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'اكتب رسالتك…' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('self-hosted Readex Pro covers both scripts and controls without browser font service requests', async ({ page }) => {
  const externalFonts: string[] = [];
  page.on('request', (request) => {
    if (/^https:\/\/fonts\.(googleapis|gstatic)\.com\//.test(request.url())) externalFonts.push(request.url());
  });
  await page.goto('/');
  const font = await page.evaluate(async () => {
    await document.fonts.ready;
    const family = getComputedStyle(document.body).fontFamily;
    const faces = await document.fonts.load(`400 16px ${family.split(',')[0]}`, 'Contact center مركز الخدمة');
    return {
      family,
      loadedFaces: faces.filter((face) => face.status === 'loaded').length,
      controls: [...document.querySelectorAll('button, select, textarea')].map((element) => getComputedStyle(element).fontFamily),
    };
  });
  expect(font.family).toMatch(/Readex[ _]Pro/);
  expect(font.loadedFaces).toBeGreaterThanOrEqual(2);
  for (const family of font.controls) expect(family).toBe(font.family);
  await page.getByRole('button', { name: 'Change language', exact: true }).click();
  expect(await page.getByRole('heading', { name: 'كيف يمكننا مساعدتك؟' }).evaluate((heading) => getComputedStyle(heading).fontFamily)).toBe(font.family);
  expect(externalFonts).toEqual([]);
});

test('message text does not implicitly change the UI locale or select a profile', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Change language', exact: true }).click();
  await page.getByRole('textbox', { name: 'اكتب رسالتك…' }).fill('Please help me.');
  const request = page.waitForRequest('**/backend/conversation/**/stream');
  await page.getByRole('button', { name: 'إرسال الرسالة', exact: true }).click();
  expect((await request).postDataJSON()).toMatchObject({ locale: 'ar', profile_id: null });
  await expect(page.locator('html')).toHaveAttribute('lang', 'ar');
});

test('clearing a stored preference restores the configured default', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('contact-center-language', 'ar'));
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('lang', 'ar');
  await page.evaluate(() => {
    localStorage.removeItem('contact-center-language');
    window.dispatchEvent(new StorageEvent('storage', { key: 'contact-center-language', newValue: null }));
  });
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.locator('html')).toHaveAttribute('dir', 'ltr');
});

test('disabled representative module ignores streamed handover events without HTTP or WebSocket side effects', async ({ page }) => {
  const handovers: string[] = [];
  const sockets: string[] = [];
  page.on('request', (request) => { if (/\/(request-handover|handover-status|complete-handover)$/.test(request.url())) handovers.push(request.url()); });
  page.on('websocket', (socket) => { if (socket.url().includes('/backend/')) sockets.push(socket.url()); });
  await page.route('**/backend/conversation/**/stream', (route) => route.fulfill({
    contentType: 'application/x-ndjson',
    body: [
      ['handover_suggested', { reason: 'Help requested' }], ['handover_requested', {}],
      ['delta', { content: 'The chat reply remains available.' }], ['end', {}],
    ].map((item) => JSON.stringify(item)).join('\n') + '\n',
  }));
  await page.goto('/');
  await page.getByRole('textbox', { name: 'Type a message…' }).fill('Please help.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByText('The chat reply remains available.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Request human agent', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Yes, connect me', exact: true })).toHaveCount(0);
  await expect(page.getByText('Connecting you with a human agent…', { exact: true })).toHaveCount(0);
  expect(handovers).toEqual([]);
  expect(sockets).toEqual([]);
});
