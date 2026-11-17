import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const neutral = JSON.parse(fs.readFileSync(path.join(root, 'src/config/template.json'), 'utf8'));

function harness({ config = structuredClone(neutral), api = 'https://api.test', imports = {}, globals = {} } = {}) {
  const cache = new Map();
  const motion = new Proxy({}, { get: (_, tag) => ({ children, ...props }) => React.createElement(tag,
    Object.fromEntries(Object.entries(props).filter(([key]) => !['initial', 'animate', 'exit', 'transition', 'whileHover', 'whileTap', 'layout'].includes(key))), children) });
  const overrides = {
    'framer-motion': { motion, AnimatePresence: ({ children }) => children },
    'next/navigation': { useRouter: () => ({ push() {} }) },
    'next/image': { __esModule: true, default: (props) => React.createElement('img',
      Object.fromEntries(Object.entries(props).filter(([key]) => key !== 'unoptimized'))) },
    '@/config/endpoints': { API_BASE_URL: api },
    '@/i18n/LocaleProvider': {
      LocaleProvider: ({ children }) => children,
      useLocale: () => ({
        locale: config.locale.default, setLocale() {},
        t: (key, values) => load('src/i18n/messages.ts').translate(config.locale.default, key, values),
      }),
    },
    ...imports,
  };
  function load(relative) {
    let filename = path.resolve(root, relative);
    if (!path.extname(filename)) filename = ['.ts', '.tsx'].map((suffix) => filename + suffix).find(fs.existsSync);
    if (filename.endsWith('/config/template.json')) return config;
    if (cache.has(filename)) return cache.get(filename).exports;
    const loaded = { exports: {} };
    cache.set(filename, loaded);
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
    }).outputText;
    vm.runInNewContext(source, {
      module: loaded, exports: loaded.exports, console: { log() {}, warn() {}, error() {} },
      setTimeout, clearTimeout, setInterval, clearInterval,
      fetch: () => assert.fail('Unexpected HTTP request'), ...globals,
      require(name) {
        if (name.endsWith('.css')) return {};
        if (Object.hasOwn(overrides, name)) return overrides[name];
        if (name.startsWith('@/')) return load(`src/${name.slice(2)}`);
        if (name.startsWith('.')) return load(path.relative(root, path.resolve(path.dirname(filename), name)));
        return require(name);
      },
    }, { filename });
    return loaded.exports;
  }
  return { load, render: (component, props) => renderToStaticMarkup(React.createElement(component, props)) };
}

for (const feature of ['chat', 'agentConsole']) {
  test(`disabled ${feature} never mounts the login form, queue, dashboard hook or backend`, () => {
    const config = structuredClone(neutral);
    config.features.chat = true;
    config.features.agentConsole = true;
    config.features[feature] = false;
    const app = harness({ config, imports: {
      '@/stores/agentStore': { useAgentStore: () => assert.fail('Disabled dashboard mounted a store consumer') },
      '@/hooks/useAgentWebSocket': { useAgentWebSocket: () => assert.fail('Disabled dashboard mounted transport') },
    } });
    for (const page of ['src/app/page.tsx', 'src/app/dashboard/page.tsx']) {
      const markup = app.render(app.load(page).default);
      assert.match(markup, /Contact center/);
      assert.match(markup, /is not enabled/);
      assert.doesNotMatch(markup, /<form|Waiting queue/);
    }
  });
}

test('custom branding, supported locale, welcome and metadata are shared by login and console', () => {
  const config = structuredClone(neutral);
  config.features.agentConsole = true;
  config.locale.default = 'ar';
  config.locale.supported = ['ar'];
  config.brand.name.ar = 'مساحة العمل';
  config.brand.assistantName.ar = 'مساعد الخدمة';
  config.brand.logoUrl = '/globe.svg';
  config.brand.colors = { primary: '#123456', accent: '#abc' };
  config.welcome.title.ar = 'مرحباً بك';
  config.welcome.description.ar = 'اختر قناة.';
  const app = harness({ config, imports: { '@/stores/agentStore': { useAgentStore: () => ({ login() {} }) } } });
  const layout = app.load('src/app/layout.tsx');
  const markup = app.render(layout.default, { children: React.createElement(app.load('src/app/page.tsx').default) });
  assert.match(markup, /lang="ar" dir="rtl"/);
  assert.match(markup, /--brand-primary:#123456/);
  assert.match(markup, /--brand-accent-rgb:170 187 204/);
  assert.match(markup, /src="\/globe.svg"/);
  assert.match(markup, /مساحة العمل/);
  assert.match(markup, /مرحباً بك/);
  assert.match(markup, /اختر قناة/);
  assert.doesNotMatch(markup, /تغيير اللغة/);
  assert.equal(layout.metadata.title, 'مساحة العمل | وحدة تحكم الموظف');
});

test('prepared PNG, JPEG and WebP logos remain same-origin public URLs', () => {
  for (const extension of ['png', 'jpg', 'webp']) {
    const config = structuredClone(neutral);
    config.brand.logoUrl = `/brand-logo.${extension}`;
    const app = harness({ config });
    const markup = app.render(app.load('src/components/Brand.tsx').Brand);
    assert.ok(markup.includes(`src="${config.brand.logoUrl}"`));
    assert.equal(app.load('src/app/layout.tsx').metadata.icons.icon, config.brand.logoUrl);
    assert.equal(Object.hasOwn(config.brand, 'logoFile'), false);
  }
});

function transport({ chat = true, agentConsole = true, api = 'https://api.test' } = {}) {
  const config = structuredClone(neutral);
  config.features.chat = chat;
  config.features.agentConsole = agentConsole;
  const sockets = [];
  const requests = [];
  const slots = [];
  const effects = [];
  const cleanups = [];
  let cursor = 0;
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    open() { this.readyState = 1; this.onopen?.(); }
    message(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
  }
  const hooks = {
    useCallback: (callback) => callback,
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], (value) => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
    },
    useRef(initial) {
      const index = cursor++;
      return slots[index] ??= { current: initial };
    },
    useEffect(effect, dependencies) {
      const index = cursor++;
      if (!slots[index] || dependencies.some((value, i) => value !== slots[index][i])) {
        slots[index] = dependencies;
        effects.push(effect);
      }
    },
  };
  const store = {
    activeSession: null,
    setConnected() {},
    setActiveSession(session) { store.activeSession = session; },
    addMessage(message) { store.activeSession.messages.push(message); },
  };
  const app = harness({ config, api, imports: { react: hooks, '@/stores/agentStore': { useAgentStore: () => store } },
    globals: {
      WebSocket: Socket,
      setInterval: () => 1, clearInterval() {},
      fetch: async (url) => {
        requests.push(url);
        return { ok: true, json: async () => [{ role: 'user', content: 'Please help.' }, { role: 'assistant', content: 'How can I help?' }] };
      },
    },
  });
  const hook = app.load('src/hooks/useAgentWebSocket.ts').useAgentWebSocket;
  const render = (enabled = true) => {
    cursor = 0;
    const result = hook({ agentId: 'test-agent', enabled });
    while (effects.length) {
      const cleanup = effects.shift()();
      if (cleanup) cleanups.push(cleanup);
    }
    return result;
  };
  return { sockets, requests, store, render, stop: () => cleanups.forEach((cleanup) => cleanup()) };
}

for (const options of [{ chat: false }, { agentConsole: false }, { api: '' }]) {
  test(`transport makes no request when ${options.chat === false ? 'chat is disabled' : options.agentConsole === false ? 'agentConsole is disabled' : 'the endpoint is absent'}`, (t) => {
    const client = transport(options);
    t.after(client.stop);
    client.render();
    assert.equal(client.sockets.length, 0);
    assert.equal(client.requests.length, 0);
  });
}

test('chat queue, accept, history, messages and handover completion retain their wire contract', async (t) => {
  const client = transport();
  t.after(client.stop);
  client.render();
  const socket = client.sockets[0];
  assert.equal(socket.url, 'wss://api.test/ws/agent/test-agent');
  socket.open();
  socket.message({ type: 'queue_update', queue: [{ session_id: 'test-session', waiting_seconds: 12, queued_at: new Date().toISOString() }] });
  let hook = client.render();
  assert.equal(hook.isConnected, true);
  assert.equal(hook.queue[0].sessionId, 'test-session');
  hook.acceptSession('test-session');
  assert.deepEqual(socket.sent[0], { type: 'accept_session', session_id: 'test-session' });
  socket.message({ type: 'session_assigned', session_id: 'test-session' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(client.requests, ['https://api.test/conversation/test-session']);
  assert.equal(client.store.activeSession.messages.length, 2);
  hook = client.render();
  hook.sendMessage('Here to help.');
  assert.deepEqual(socket.sent[1], { type: 'chat_message', session_id: 'test-session', content: 'Here to help.' });
  socket.message({ type: 'chat_message', sender_type: 'customer', content: 'Thank you.' });
  assert.equal(client.store.activeSession.messages.at(-1).content, 'Thank you.');
  hook.completeSession();
  assert.deepEqual(socket.sent[2], { type: 'complete_session', session_id: 'test-session' });
  assert.equal(client.store.activeSession, null);
});

test('unmounting the agent queue closes the socket without leaving reconnect handlers', () => {
  const client = transport();
  client.render();
  client.sockets[0].open();
  client.stop();
  assert.equal(client.sockets[0].readyState, 3);
  assert.equal(client.sockets[0].onclose, null);
  assert.equal(client.sockets[0].onmessage, null);
});
