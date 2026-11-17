import { expect, test } from './fixtures';

test.use({ baseURL: 'http://127.0.0.1:38472' });

test.beforeEach(async ({ page }) => {
  await page.route('**/backend/profiles', (route) => route.fulfill({
    json: [{ id: 'test-record', display_name: 'Backend record', display_name_ar: 'سجل من الخدمة', phone: null }],
  }));
});

test('private configuration supplies branding, layout, Arabic defaults, prompts and backend profiles', async ({ page }) => {
  await page.route('**/backend/conversation/**/stream', (route) => route.fulfill({
    contentType: 'application/x-ndjson',
    body: '[\"start\",\"assistant\"]\n[\"delta\",{\"content\":\"A configured reply.\"}]\n[\"end\",{}]\n',
  }));
  const serverHtml = await (await page.request.get('/')).text();
  expect(serverHtml).toMatch(/<html[^>]*lang="ar"[^>]*dir="rtl"/);
  await page.goto('/');
  await expect(page).toHaveTitle('مساحة العمل | مساعد الخدمة');
  await expect(page.getByRole('heading', { name: 'مساحة العمل' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'مرحباً بك' })).toBeVisible();
  await expect(page.getByText('اختر قناة.', { exact: true })).toBeVisible();
  await expect(page.getByRole('img', { name: 'مساحة العمل' })).toHaveAttribute('src', '/globe.svg');
  expect(await page.locator('html').evaluate((element) => getComputedStyle(element).getPropertyValue('--brand-primary'))).toBe('#123456');
  expect(await page.locator('html').evaluate((element) => getComputedStyle(element).getPropertyValue('--brand-accent'))).toBe('#aabbcc');
  await expect(page.locator('[data-layout="voice-first"]')).toHaveCount(1);
  const panes = await page.evaluate(() => ({
    voice: document.querySelector('aside')!.getBoundingClientRect().width,
    chat: document.querySelector('main')!.getBoundingClientRect().width,
  }));
  expect(panes.voice).toBeGreaterThan(panes.chat);
  await expect(page.getByLabel('رمز دولة المستفيد', { exact: true })).toHaveValue('+1');
  await page.getByLabel('الملف الشخصي', { exact: true }).selectOption('test-record');
  const request = page.waitForRequest('**/backend/conversation/**/stream');
  await page.getByRole('button', { name: 'طلب المساعدة', exact: true }).click();
  expect((await request).postDataJSON()).toMatchObject({ message: 'أحتاج المساعدة.', locale: 'ar', profile_id: 'test-record' });
  await expect(page.getByText('A configured reply.', { exact: true })).toBeVisible();
  await expect(page.getByLabel('الملف الشخصي', { exact: true })).toBeDisabled();
});

test('localized phone errors preserve the outbound call payload without placing a real call', async ({ page }) => {
  await page.route('**/voice-api/call', (route) => route.fulfill({ status: 503, body: 'gateway unavailable' }));
  await page.goto('/');
  await page.getByLabel('رقم المستفيد', { exact: true }).fill('2025550123');
  await page.getByLabel('رقم الموظف للتحويل', { exact: true }).fill('2025550124');
  const request = page.waitForRequest('**/voice-api/call');
  await page.getByRole('button', { name: 'إجراء المكالمة', exact: true }).click();
  expect((await request).postDataJSON()).toEqual({ number: '+12025550123', operator_number: '+12025550124', language: 'ar' });
  await expect(page.getByText('الخدمة الصوتية غير متاحة حالياً. يرجى المحاولة مرة أخرى.', { exact: true })).toBeVisible();
  await expect(page.getByText('gateway unavailable', { exact: true })).not.toBeVisible();
});

test('an absent dialing country never silently supplies a country code', async ({ page }) => {
  const calls: string[] = [];
  page.on('request', (request) => { if (request.url().endsWith('/voice-api/call')) calls.push(request.url()); });
  await page.goto('/');
  await page.getByLabel('رمز دولة المستفيد', { exact: true }).fill('');
  await page.getByLabel('رقم المستفيد', { exact: true }).fill('2025550123');
  await page.getByRole('button', { name: 'إجراء المكالمة', exact: true }).click();
  await expect(page.getByText('أدخل رقم هاتف دولياً صحيحاً.', { exact: true })).toBeVisible();
  expect(calls).toEqual([]);
});

test('profile backend failure does not fabricate a record or guest list', async ({ page }) => {
  await page.route('**/backend/profiles', (route) => route.fulfill({ status: 503 }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'مرحباً بك' })).toBeVisible();
  await expect(page.getByLabel('الملف الشخصي', { exact: true })).toHaveCount(0);
});

test('configured voice and phone remain reachable on narrow screens while switching language', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: 'تغيير اللغة', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Welcome to the workspace' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start talking', exact: true })).toHaveCount(1);
  await page.getByRole('button', { name: 'Place call', exact: true }).scrollIntoViewIfNeeded();
  await expect(page.getByRole('button', { name: 'Place call', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('selected representative module requests and completes a human handover through the existing protocol', async ({ page }) => {
  const messages: object[] = [];
  await page.route('**/backend/conversation/**/stream', (route) => route.fulfill({
    contentType: 'application/x-ndjson',
    body: '[\"start\",\"assistant\"]\n[\"delta\",{\"content\":\"A configured reply.\"}]\n[\"end\",{}]\n',
  }));
  await page.route('**/backend/conversation/**/request-handover', (route) => route.fulfill({ json: { success: true } }));
  await page.routeWebSocket('**/backend/ws/customer/**', (socket) => {
    socket.send(JSON.stringify({ type: 'queue_position', position: 1 }));
    socket.send(JSON.stringify({ type: 'agent_assigned', agent_id: 'test-agent' }));
    socket.onMessage((data) => {
      messages.push(JSON.parse(String(data)));
      socket.send(JSON.stringify({ type: 'chat_message', sender_type: 'agent', content: 'An agent reply.' }));
      socket.send(JSON.stringify({ type: 'handover_completed' }));
    });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'تغيير اللغة', exact: true }).click();
  await page.getByRole('textbox', { name: 'Type a message…' }).fill('Please help.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await page.getByRole('button', { name: 'Request human agent', exact: true }).click();
  await expect(page.getByText('You are now chatting with a live agent.', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: 'Type a message…' }).fill('Hello agent.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByText('An agent reply.', { exact: true })).toBeVisible();
  expect(messages).toContainEqual({ type: 'chat_message', content: 'Hello agent.' });
  await expect(page.getByText('You are now chatting with the assistant again.', { exact: true })).toBeVisible();
});
