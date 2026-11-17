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

function harness({ config = structuredClone(neutral), locale = config.locale.default, api = 'https://api.test', fetch, imports = {}, globals = {} } = {}) {
  const cache = new Map();
  const effects = [];
  const buttons = [];
  const calls = { chat: 0, voice: 0, profiles: 0, sent: [] };
  const motion = new Proxy({}, { get: (_, tag) => ({ children, ...props }) => {
    if (tag === 'button') buttons.push({ children, ...props });
    const attributes = Object.fromEntries(Object.entries(props).filter(([key]) =>
      !['initial', 'animate', 'exit', 'transition', 'variants', 'whileHover', 'whileTap', 'layout'].includes(key)));
    return React.createElement(tag, attributes, children);
  } });
  const overrides = {
    react: { ...React, useEffect: (effect) => effects.push(effect) },
    'framer-motion': { motion, AnimatePresence: ({ children }) => children },
    'next/image': { default: (props) => React.createElement('img', Object.fromEntries(Object.entries(props).filter(([key]) => key !== 'unoptimized'))), __esModule: true },
    'next/font/google': { Readex_Pro: () => ({ variable: 'readex' }) },
    '@/constants': { API_BASE_URL: api, VOICE_BASE_URL: api, VOICES: ['mul_nourah'] },
    '@/i18n/LocaleProvider': {
      LocaleProvider: ({ children }) => children,
      useLocale: () => ({
        locale, setLocale() {},
        t: (key, values) => load('src/i18n/messages.ts').translate(locale, key, values),
        localize: (text) => text,
      }),
    },
    '@/hooks/useChat': { useChat() {
      calls.chat++;
      return { messages: [], handover: {}, send: (text) => calls.sent.push(text) };
    } },
    '@/hooks/useRealtimeVoice': { useRealtimeVoice() {
      calls.voice++;
      return { status: 'idle', levelRef: { current: 0 }, start() {}, stop() {}, setLanguage() {}, toggleMute() {} };
    } },
    '@/stores/chatStore': { useChatStore: () => ({
      profiles: [], loadProfiles: () => { calls.profiles++; },
    }) },
    ...imports,
  };
  function load(relative) {
    let filename = path.resolve(root, relative);
    if (!path.extname(filename)) {
      filename = ['.ts', '.tsx', '/index.ts', '/index.tsx'].map((suffix) => filename + suffix).find(fs.existsSync);
    }
    if (filename.endsWith('/config/template.json')) return config;
    if (cache.has(filename)) return cache.get(filename).exports;
    const loaded = { exports: {} };
    cache.set(filename, loaded);
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
    }).outputText;
    vm.runInNewContext(source, {
      module: loaded, exports: loaded.exports, console: { log() {}, warn() {}, error() {} },
      fetch: fetch ?? (() => { throw new Error('Unexpected request'); }),
      TextDecoder, URL, Date, setTimeout, clearTimeout, ...globals,
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
  return { load, calls, effects, buttons, render: (component, props) => renderToStaticMarkup(React.createElement(component, props)) };
}

test('all frontends ship the same neutral contract, with no records or suggested prompts', () => {
  const agent = JSON.parse(fs.readFileSync(path.join(root, '../agent_console/src/config/template.json'), 'utf8'));
  assert.deepEqual(agent, neutral);
  const assist = JSON.parse(fs.readFileSync(path.join(root, '../../assist/apps/console/src/config/template.json'), 'utf8'));
  assert.deepEqual(assist, neutral);
  assert.equal(neutral.schemaVersion, 1);
  assert.equal(neutral.brand.name.en, 'Contact center');
  assert.equal(neutral.brand.logoUrl, null);
  assert.deepEqual(neutral.features, { chat: true, voice: false, phone: false, profiles: false, agentConsole: false, agentAssist: false });
  assert.deepEqual(neutral.quickActions, []);
  assert.equal(neutral.locale.dialingCountryCode, '');
});

for (let mask = 0; mask < 16; mask++) {
  test(`feature selection ${mask}: disabled children never mount or load profiles`, () => {
    const config = structuredClone(neutral);
    config.features = { ...config.features, ...Object.fromEntries(['chat', 'voice', 'phone', 'profiles'].map((key, index) => [key, !!(mask & (1 << index))])) };
    const app = harness({ config });
    const markup = app.render(app.load('src/components/AppShell.tsx').AppShell);
    for (const effect of app.effects) effect();
    assert.equal(app.calls.chat, Number(config.features.chat));
    assert.equal(app.calls.voice, Number(config.features.voice));
    assert.equal(app.calls.profiles, Number(config.features.chat && config.features.profiles));
    assert.equal(markup.includes('id="customer-phone"'), config.features.phone);
    assert.ok(markup.includes('<main'), 'every feature combination retains a main landmark');
    assert.equal(markup.includes('No channels are enabled.'), !(config.features.chat || config.features.voice || config.features.phone));
  });
}

test('private branding, colors, logo, welcome and localized prompts reach the rendered UI', () => {
  const config = structuredClone(neutral);
  config.brand = {
    name: { en: 'Configured workspace', ar: 'مساحة العمل' },
    assistantName: { en: 'Configured assistant', ar: 'مساعد الخدمة' },
    logoUrl: 'https://assets.example.test/logo.svg',
    colors: { primary: '#123456', accent: '#abcdef' },
    layout: 'voice-first',
  };
  config.welcome = {
    title: { en: 'Welcome to the workspace', ar: 'مرحباً بك' },
    description: { en: 'Choose a channel.', ar: 'اختر قناة.' },
  };
  config.quickActions = [{ id: 'help', label: { en: 'Get help', ar: 'طلب المساعدة' }, prompt: { en: 'Please help.', ar: 'أحتاج المساعدة.' } }];
  for (const locale of ['en', 'ar']) {
    const app = harness({ config, locale });
    const shell = React.createElement(app.load('src/components/AppShell.tsx').AppShell);
    const layout = app.load('src/app/layout.tsx');
    const markup = app.render(layout.default, { children: shell });
    assert.match(markup, /--brand-primary:#123456;--brand-accent:#abcdef/);
    assert.match(markup, /data-layout="voice-first"/);
    assert.ok(markup.includes(config.brand.name[locale]));
    assert.ok(markup.includes(config.brand.assistantName[locale]));
    assert.ok(markup.includes(config.brand.logoUrl));
    assert.ok(markup.includes(config.welcome.title[locale]));
    assert.ok(markup.includes(config.welcome.description[locale]));
    app.buttons.find((button) => button.children === config.quickActions[0].label[locale]).onClick();
    assert.deepEqual(app.calls.sent, [config.quickActions[0].prompt[locale]]);
    assert.equal(layout.metadata.title, 'Configured workspace | Configured assistant');
  }
});

test('chat-first collapses calls initially and unsupported language controls are absent', () => {
  const config = structuredClone(neutral);
  config.features.voice = true;
  config.brand.layout = 'chat-first';
  config.locale = { default: 'ar', supported: ['ar'], dialingCountryCode: '' };
  const app = harness({ config });
  const markup = app.render(app.load('src/components/AppShell.tsx').AppShell);
  assert.equal(app.calls.voice, 0);
  assert.ok(markup.includes('فتح لوحة الاتصال'));
  assert.ok(!markup.includes('تغيير اللغة'));
  assert.equal(app.load('src/config/template.ts').isSupportedLocale('en'), false);
});

test('configured primary colors retain readable button foregrounds', () => {
  for (const [color, foreground] of [['#fff', '#000000'], ['#000000', '#ffffff']]) {
    const config = structuredClone(neutral);
    config.brand.colors.primary = color;
    const app = harness({ config });
    assert.equal(app.load('src/config/template.ts').brandStyles['--brand-on-primary'], foreground);
  }
});

test('prepared same-origin raster logos reach the header and metadata without private paths', () => {
  for (const extension of ['png', 'jpg', 'webp']) {
    const config = structuredClone(neutral);
    config.brand.logoUrl = `/brand-logo.${extension}`;
    const app = harness({ config });
    const markup = app.render(app.load('src/components/layout/Header.tsx').Header);
    assert.ok(markup.includes(`src="${config.brand.logoUrl}"`));
    assert.equal(app.load('src/app/layout.tsx').metadata.icons.icon, config.brand.logoUrl);
    assert.equal(Object.hasOwn(config.brand, 'logoFile'), false);
  }
});

for (const enabled of [false, true]) {
  test(`phone permission ${enabled}: an entered destination needs no chat, CRM or profile selection`, async () => {
    const config = structuredClone(neutral);
    config.features.chat = false;
    config.features.phone = enabled;
    config.features.profiles = false;
    const states = [];
    let cursor = 0;
    const requests = [];
    const app = harness({ config, api: 'https://voice.test', imports: {
      react: { ...React, useState(initial) {
        const index = cursor++;
        states[index] = index === 1 ? '+12025550123' : initial;
        return [states[index], (value) => { states[index] = value; }];
      } },
      '@/stores/chatStore': { useChatStore: () => assert.fail('Phone dialing must not load a customer profile') },
    }, fetch: async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      return { ok: true };
    } });
    const tree = app.load('src/components/voice/PhoneDialer.tsx').PhoneDialer();
    const findCallButton = (element) => {
      if (!element || typeof element !== 'object') return null;
      if (element.type === 'button' && element.props.children === 'Place call') return element;
      const children = Array.isArray(element.props?.children) ? element.props.children : [element.props?.children];
      return children.map(findCallButton).find(Boolean);
    };
    await findCallButton(tree).props.onClick();
    assert.equal(requests.length, Number(enabled));
    if (enabled) {
      assert.deepEqual(requests[0], {
        url: 'https://voice.test/call', body: { number: '+12025550123', language: 'en' },
      });
      assert.equal(states[4].key, 'Calling {number}. Please answer your phone.');
    }
  });
}

for (const missingEndpoint of [false, true]) {
  test(`${missingEndpoint ? 'unconfigured' : 'disabled'} services never issue HTTP requests`, async () => {
    const config = structuredClone(neutral);
    config.features = { ...config.features, chat: missingEndpoint, profiles: true };
    let requests = 0;
    const app = harness({ config, api: missingEndpoint ? '' : 'https://api.test', fetch: () => { requests++; } });
    const api = app.load('src/services/api.ts');
    let error = '';
    await api.sendMessage({ conversationId: 'test', message: 'Hello', onToken() {}, onComplete() {}, onError: (message) => { error = message; } });
    assert.equal(error, 'Chat service is not configured.');
    assert.equal((await api.getProfiles()).length, 0);
    assert.equal(await api.getProfile('test'), null);
    assert.equal(await api.checkHealth(), false);
    await assert.rejects(api.requestHandover('test'), /not configured/);
    await assert.rejects(api.completeHandover('test'), /not configured/);
    await assert.rejects(api.getHandoverStatus('test'), /not configured/);
    assert.equal(requests, 0);
  });
}

test('profile choices come only from the backend, never a fallback record', async () => {
  const config = structuredClone(neutral);
  config.features.profiles = true;
  let failed = false;
  const profiles = [{ id: 'test-record', display_name: 'Backend record', phone: null }];
  const app = harness({ config, fetch: async () => ({ ok: !failed, status: 503, json: async () => profiles }) });
  const api = app.load('src/services/api.ts');
  assert.deepEqual(await api.getProfiles(), profiles);
  failed = true;
  assert.equal((await api.getProfiles()).length, 0);
  const disabled = harness({ fetch: () => assert.fail('Profiles are disabled') });
  assert.equal((await disabled.load('src/services/api.ts').getProfiles()).length, 0);
});

test('profile-disabled chat sends no stale profile identity and preserves the streaming protocol', async () => {
  let payload;
  let content = '';
  let complete = false;
  const app = harness({ fetch: async (_url, options) => {
    payload = JSON.parse(options.body);
    const data = new TextEncoder().encode([
      ['start', 'assistant'], ['delta', { content: 'Hello' }], ['end', {}],
    ].map(JSON.stringify).join('\n') + '\n');
    let read = false;
    return { ok: true, body: { getReader: () => ({ read: async () => {
      if (read) return { done: true };
      read = true;
      return { done: false, value: data };
    } }) } };
  } });
  await app.load('src/services/api.ts').sendMessage({
    conversationId: 'test', message: 'Hello', profileId: 'stale-record',
    onToken: (token) => { content += token; }, onComplete: () => { complete = true; }, onError: assert.fail,
  });
  assert.equal(payload.profile_id, null);
  assert.equal(payload.locale, 'en');
  assert.equal(content, 'Hello');
  assert.equal(complete, true);
});

test('unselected representative handover has no buttons, banners, HTTP actions or socket connection', async () => {
  const sockets = [];
  const app = harness({ imports: {
    '@/hooks/useChat': { useChat: () => ({
      messages: [{ id: 'test', role: 'user', content: 'Please help.', timestamp: new Date() }],
      handover: { status: 'suggested', suggestedReason: 'Test handover' },
      send() {}, requestHandover() {}, dismissHandoverSuggestion() {},
    }) },
    './MessageList': { MessageList: () => React.createElement('p', null, 'Existing conversation') },
  } });
  const markup = app.render(app.load('src/components/chat/ChatContainer.tsx').ChatContainer);
  assert.ok(markup.includes('Existing conversation'));
  assert.ok(!markup.includes('Request human agent'));
  assert.ok(!markup.includes('Yes, connect me'));
  const api = app.load('src/services/api.ts');
  await assert.rejects(api.requestHandover('saved-session'), /Agent console is not enabled/);
  await assert.rejects(api.getHandoverStatus('saved-session'), /Agent console is not enabled/);
  await assert.rejects(api.completeHandover('saved-session'), /Agent console is not enabled/);

  const effects = [];
  const hook = harness({ imports: {
    react: {
      useState: (value) => [value, () => {}], useRef: (current) => ({ current }),
      useCallback: (callback) => callback, useEffect: (effect) => effects.push(effect),
    },
    '../stores/chatStore': { useChatStore: () => ({ addMessage() {}, setHandoverState() {} }) },
  }, globals: {
    WebSocket: class { constructor(url) { sockets.push(url); } },
    clearInterval() {},
  } });
  hook.load('src/hooks/useWebSocket.ts').useWebSocket({ sessionId: 'saved-session', enabled: true });
  const cleanups = effects.map((effect) => effect());
  for (const cleanup of cleanups) cleanup?.();
  assert.equal(sockets.length, 0);
});

for (const enabled of [false, true]) {
  test(`streamed handover events are ${enabled ? 'preserved when selected' : 'ignored when unselected'}`, async () => {
    const config = structuredClone(neutral);
    config.features.agentConsole = enabled;
    let suggested = 0;
    let requested = 0;
    const app = harness({ config, fetch: async () => {
      const value = new TextEncoder().encode([
        ['handover_suggested', { reason: 'Help requested' }], ['handover_requested', {}], ['end', {}],
      ].map(JSON.stringify).join('\n') + '\n');
      let done = false;
      return { ok: true, body: { getReader: () => ({ read: async () => {
        if (done) return { done: true };
        done = true;
        return { done: false, value };
      } }) } };
    } });
    await app.load('src/services/api.ts').sendMessage({
      conversationId: 'test', message: 'Please help.', onToken() {}, onComplete() {}, onError: assert.fail,
      onHandoverSuggested: () => { suggested++; }, onHandoverRequested: () => { requested++; },
    });
    assert.equal(suggested, Number(enabled));
    assert.equal(requested, Number(enabled));
  });
}

test('customer handover teardown cannot reconnect a hidden or unmounted chat', () => {
  const config = structuredClone(neutral);
  config.features.agentConsole = true;
  const sockets = [];
  let reconnects = 0;
  const effects = [];
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); }
    close(code = 1005) { this.readyState = 3; this.onclose?.({ code }); }
  }
  const app = harness({ config, imports: {
    react: {
      useState: (value) => [value, () => {}],
      useRef: (current) => ({ current }),
      useCallback: (callback) => callback,
      useEffect: (effect) => effects.push(effect),
    },
    '../stores/chatStore': { useChatStore: () => ({ addMessage() {}, setHandoverState() {} }) },
  }, globals: {
    WebSocket: Socket,
    setInterval: () => 1, clearInterval() {},
    setTimeout: () => { reconnects++; return 1; }, clearTimeout() {},
  } });
  app.load('src/hooks/useWebSocket.ts').useWebSocket({ sessionId: 'test-session', enabled: true });
  const cleanups = effects.map((effect) => effect());
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url, 'wss://api.test/ws/customer/test-session');
  for (const cleanup of cleanups) cleanup?.();
  assert.equal(sockets[0].readyState, 3);
  assert.equal(sockets[0].onclose, null);
  assert.equal(reconnects, 0);
});
