import { expect, test } from './fixtures';

test.use({ baseURL: 'http://127.0.0.1:38474' });

test.describe('unselected agent console', () => {
  test.use({ baseURL: 'http://127.0.0.1:38473' });

  test('saved login cannot mount a queue or request a representative session', async ({ page }) => {
    const requests: string[] = [];
    const sockets: string[] = [];
    page.on('request', (request) => { if (request.url().includes('/agent-backend/')) requests.push(request.url()); });
    page.on('websocket', (socket) => { if (socket.url().includes('/agent-backend/')) sockets.push(socket.url()); });
    await page.addInitScript(() => localStorage.setItem('agent-store', JSON.stringify({
      state: { agentId: 'saved-test-agent', agentName: 'Saved test agent', isLoggedIn: true }, version: 0,
    })));
    await page.goto('/dashboard');
    await expect(page.getByText('Agent console is not enabled.', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Waiting queue' })).toHaveCount(0);
    await expect(page.getByRole('textbox')).toHaveCount(0);
    await page.goto('/');
    await expect(page.getByText('Agent console is not enabled.', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Enter console', exact: true })).toHaveCount(0);
    expect(requests).toEqual([]);
    expect(sockets).toEqual([]);
  });
});

test('neutral agent login has no example identity and switches language without losing input', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Contact center' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'How can we help?' })).toBeVisible();
  await expect(page.getByLabel('Your name', { exact: true })).toHaveValue('');
  await expect(page.getByRole('img')).toHaveCount(0);
  await page.getByLabel('Agent ID', { exact: true }).fill('test-agent');
  await page.getByRole('button', { name: 'Change language' }).click();
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(page.getByLabel('معرّف الموظف', { exact: true })).toHaveValue('test-agent');
  await expect(page.getByLabel('اسمك', { exact: true })).toHaveValue('');
});

test('agent queue, history, reply and completion work with neutral branding', async ({ page }) => {
  const received: object[] = [];
  await page.route('**/agent-backend/conversation/test-session', (route) => route.fulfill({
    json: [{ role: 'user', content: 'Please help.' }, { role: 'assistant', content: 'How can I help?' }],
  }));
  await page.routeWebSocket('**/agent-backend/ws/agent/**', (socket) => {
    socket.send(JSON.stringify({ type: 'queue_update', queue: [{ session_id: 'test-session', waiting_seconds: 12, queued_at: new Date().toISOString() }] }));
    socket.onMessage((data) => {
      const message = JSON.parse(String(data));
      received.push(message);
      if (message.type === 'accept_session') {
        socket.send(JSON.stringify({ type: 'session_assigned', session_id: 'test-session' }));
        socket.send(JSON.stringify({ type: 'queue_update', queue: [] }));
      }
      if (message.type === 'chat_message') {
        socket.send(JSON.stringify({ type: 'chat_message', sender_type: 'customer', content: 'Thank you.' }));
      }
    });
  });
  await page.goto('/');
  await page.getByLabel('Agent ID', { exact: true }).fill('test-agent');
  await page.getByLabel('Your name', { exact: true }).fill('Test agent');
  await page.getByRole('button', { name: 'Enter console', exact: true }).click();
  await page.getByRole('button', { name: 'Accept handover', exact: true }).click();
  await expect(page.getByText('Please help.', { exact: true })).toBeVisible();
  await expect(page.getByText('Assistant', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: 'Type a reply…' }).fill('Here to help.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByText('Thank you.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Complete handover', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  await expect(page.getByText('No active conversation', { exact: true })).toBeVisible();
  expect(received).toContainEqual({ type: 'accept_session', session_id: 'test-session' });
  expect(received).toContainEqual({ type: 'chat_message', session_id: 'test-session', content: 'Here to help.' });
  expect(received).toContainEqual({ type: 'complete_session', session_id: 'test-session' });
});
