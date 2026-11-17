import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const tick = () => new Promise((resolve) => setImmediate(resolve));
const directory = path.dirname(fileURLToPath(import.meta.url));

test('React voice language defaults to Arabic and changes only on explicit selection', async (t) => {
  const client = clientHarness();
  t.after(client.stop);
  await client.start();
  const socket = client.sockets[0];
  socket.open();
  assert.deepEqual(socket.sent[0], { type: 'session.update', session: { language: 'ar' } });
  client.setLanguage('en');
  assert.equal(socket.sent.length, 1, 'queue the selection while the voice session is connecting');
  socket.message({ type: 'session.updated', session: { provider: 'humain' } });
  assert.deepEqual(socket.sent[1], { type: 'session.language', language: 'en' });
  client.setLanguage('en');
  assert.equal(socket.sent.length, 2, 'do not repeatedly override a subsequent spoken language choice');
  client.setLanguage('ar');
  assert.deepEqual(socket.sent[2], { type: 'session.language', language: 'ar' });
  assert.equal(client.sockets.length, 1);
  assert.equal(client.streams[0].track.stopped, false);
});

// Exercise the browser media lifecycle without microphones, network, or a DOM renderer.
function clientHarness(legacy = false, { voiceBaseUrl = 'https://voice.test' } = {}) {
  const sockets = [];
  const contexts = [];
  const processors = [];
  const streams = [];
  const states = [];
  const elements = new Map();
  const control = {};
  const node = () => ({ connect() {}, disconnect() { this.disconnected = true; } });
  const makeStream = () => {
    const track = { stopped: false, stop() { this.stopped = true; } };
    const stream = { getTracks: () => [track], track };
    streams.push(stream);
    return stream;
  };
  control.getMedia = async () => makeStream();

  class AudioContext {
    constructor() {
      this.currentTime = 0;
      this.state = 'running';
      this.destination = {};
      contexts.push(this);
    }
    async resume() {}
    async close() { this.state = 'closed'; }
    createMediaStreamSource() { return node(); }
    createScriptProcessor() {
      const processor = node();
      processors.push(processor);
      return processor;
    }
    createAnalyser() {
      return { ...node(), frequencyBinCount: 128, getByteTimeDomainData: (data) => data.fill(128) };
    }
    createBuffer(channels, length, rate) {
      const data = new Float32Array(length);
      return { duration: length / rate, getChannelData: () => data, copyToChannel: (value) => data.set(value) };
    }
    createBufferSource() { return { ...node(), start() {}, stop() {} }; }
  }

  class WebSocket {
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      sockets.push(this);
    }
    open() { this.readyState = 1; this.onopen?.(); }
    send(data) { this.sent.push(JSON.parse(data)); }
    message(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
    close() { this.readyState = 3; this.onclose?.(); }
  }

  function getElement(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        value: '', style: {}, classList: { add() {}, remove() {} }, addEventListener() {},
      });
    }
    return elements.get(id);
  }

  const exports = {};
  const context = vm.createContext({
    exports, AudioContext, WebSocket, Error, Float32Array, Int16Array, Uint8Array,
    atob, btoa, setTimeout, clearTimeout,
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    console: { log() {}, error() {}, warn() {} },
    window: { AudioContext, location: { host: 'voice.test', protocol: 'https:' } },
    navigator: { mediaDevices: { getUserMedia: () => control.getMedia() } },
    document: { getElementById: getElement, querySelectorAll: () => [] },
    fetch: async () => ({
      ok: true,
      json: async () => ({
        organization: { defaultLanguage: 'ar' },
        features: { browserVoice: true, applicationTelephony: true },
      }),
    }),
    require(name) {
      if (name === '@/constants') return { VOICE_BASE_URL: voiceBaseUrl };
      if (name === '@/i18n/messages') return { DEFAULT_LOCALE: 'ar' };
      if (name === 'react') return {
        useCallback: (fn) => fn,
        useRef: (current) => ({ current }),
        useEffect: (fn) => fn(),
        useState(initial) {
          const index = states.push(initial) - 1;
          return [initial, (value) => {
            states[index] = typeof value === 'function' ? value(states[index]) : value;
          }];
        },
      };
      throw new Error(`Unexpected import: ${name}`);
    },
  });

  let start;
  let stop;
  let mute;
  let setLanguage;
  if (legacy) {
    vm.runInContext(fs.readFileSync(path.join(directory, '../../../voice/src/app/static/app.js'), 'utf8'), context);
    start = async (voice = '') => {
      await tick();
      getElement('themeSelect').value = voice;
      context.onToggleListening();
      await tick();
    };
    stop = context.stopRecording;
    mute = context.toggleMicrophone;
  } else {
    const source = fs.readFileSync(path.join(directory, '../src/hooks/useRealtimeVoice.ts'), 'utf8');
    const compiled = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    });
    vm.runInContext(compiled.outputText, context);
    const hook = exports.useRealtimeVoice();
    start = hook.start;
    stop = hook.stop;
    mute = hook.toggleMute;
    setLanguage = hook.setLanguage;
  }
  return {
    start, stop, mute, setLanguage, sockets, contexts, processors, streams, states, elements, control, makeStream,
    emitMic() {
      processors.at(-1).onaudioprocess?.({
        inputBuffer: { getChannelData: () => new Float32Array(4096).fill(0.5) },
      });
    },
  };
}

test('React voice refuses an unconfigured endpoint before requesting microphone access', async (t) => {
  const client = clientHarness(false, { voiceBaseUrl: '' });
  t.after(client.stop);
  await client.start();
  assert.equal(client.sockets.length, 0);
  assert.equal(client.contexts.length, 0);
  assert.equal(client.streams.length, 0);
  assert.equal(client.states[0], 'error');
  assert.equal(client.states[3], 'Voice service is not configured.');
});

for (const legacy of [false, true]) {
  const name = legacy ? 'standalone browser' : 'React voice hook';

  test(`${name}: per-session voice, ready handshake, mute, and cleanup`, async (t) => {
    const client = clientHarness(legacy);
    t.after(client.stop);
    await client.start('mul_sara');
    const socket = client.sockets[0];
    socket.open();
    assert.deepEqual(socket.sent[0], { type: 'session.update', session: { voice: 'mul_sara', language: 'ar' } });
    client.emitMic();
    assert.equal(socket.sent.length, 1, 'no microphone audio before HUMAIN is ready');
    socket.message({ type: 'session.updated', session: { provider: 'humain' } });
    client.emitMic();
    assert.equal(socket.sent[1].type, 'input_audio_buffer.append');
    assert.equal(Buffer.from(socket.sent[1].audio, 'base64').length, 4096 * 2);
    client.mute();
    client.emitMic();
    assert.ok(Buffer.from(socket.sent[2].audio, 'base64').every((byte) => byte === 0));
    socket.message({ type: 'response.output_audio.delta', delta: 'AQABAA==' });
    client.stop();
    assert.ok(client.streams[0].track.stopped);
    assert.equal(client.contexts[0].state, 'closed');
    assert.ok(client.processors[0].disconnected);
    assert.equal(socket.readyState, 3);
    await client.start();
    const next = client.sockets[1];
    next.open();
    assert.deepEqual(next.sent[0], { type: 'session.update', session: { language: 'ar' } });
  });

  test(`${name}: backend errors stop the microphone and preserve an error`, async (t) => {
    const client = clientHarness(legacy);
    t.after(client.stop);
    await client.start();
    const socket = client.sockets[0];
    socket.open();
    socket.message({ type: 'error', error: { message: 'HUMAIN authorization was rejected.' } });
    assert.ok(client.streams[0].track.stopped);
    assert.equal(client.contexts[0].state, 'closed');
    if (legacy) {
      assert.match(client.elements.get('statusMessage').textContent, /authorization/);
    } else {
      assert.equal(client.states[0], 'error');
      assert.match(client.states[3], /authorization/);
    }
  });

  test(`${name}: optional metadata warnings do not interrupt audio`, async (t) => {
    const client = clientHarness(legacy);
    t.after(client.stop);
    await client.start();
    const socket = client.sockets[0];
    socket.open();
    const message = 'HUMAIN did not apply some optional settings. The conversation can continue.';
    socket.message({ type: 'session.warning', warning: { code: 'humain_ignored_metadata', message } });
    socket.message({ type: 'session.updated', session: { provider: 'humain' } });
    if (legacy) {
      assert.equal(client.elements.get('warningMessage').textContent, message);
      assert.equal(client.elements.get('warningMessage').hidden, false);
      assert.equal(client.elements.get('statusMessage').textContent, 'Listening');
    } else {
      assert.equal(client.states[4], message);
      assert.equal(client.states[0], 'listening');
      assert.equal(client.states[3], null);
    }
    socket.message({ type: 'session.warning', warning: { code: 'humain_ignored_metadata', message } });
    client.emitMic();
    socket.message({ type: 'response.output_audio.delta', delta: 'AQABAA==' });
    assert.equal(socket.sent[1].type, 'input_audio_buffer.append');
    assert.equal(socket.readyState, 1);
    assert.equal(client.streams[0].track.stopped, false);
    assert.equal(client.contexts[0].state, 'running');
    socket.message({ type: 'session.warning', warning: null });
    if (legacy) {
      assert.equal(client.elements.get('warningMessage').textContent, '');
      assert.equal(client.elements.get('warningMessage').hidden, true);
    } else {
      assert.equal(client.states[4], null);
    }
    socket.message({ type: 'session.warning', warning: { message } });
    client.stop();
    await client.start();
    if (legacy) {
      assert.equal(client.elements.get('warningMessage').textContent, '');
    } else {
      assert.equal(client.states[4], null);
    }
  });

  test(`${name}: unexpected socket close releases audio`, async (t) => {
    const client = clientHarness(legacy);
    t.after(client.stop);
    await client.start();
    client.sockets[0].open();
    client.sockets[0].close();
    assert.ok(client.streams[0].track.stopped);
    assert.equal(client.contexts[0].state, 'closed');
  });

  test(`${name}: late microphone permission cannot revive a stopped session`, async (t) => {
    const client = clientHarness(legacy);
    t.after(client.stop);
    let resolvePermission;
    client.control.getMedia = () => new Promise((resolve) => { resolvePermission = resolve; });
    const starting = client.start();
    await tick();
    client.stop();
    const lateStream = client.makeStream();
    resolvePermission(lateStream);
    await starting;
    await tick();
    assert.ok(lateStream.track.stopped);
    assert.equal(client.sockets.length, 0);
  });

  test(`${name}: a stale permission failure cannot stop a newer session`, async (t) => {
    const client = clientHarness(legacy);
    t.after(client.stop);
    let rejectPermission;
    client.control.getMedia = () => new Promise((resolve, reject) => { rejectPermission = reject; });
    const oldStart = client.start();
    await tick();
    client.stop();
    client.control.getMedia = async () => client.makeStream();
    await client.start();
    client.sockets[0].open();
    rejectPermission(new Error('Permission denied for previous attempt'));
    await oldStart;
    await tick();
    assert.equal(client.sockets[0].readyState, 1);
    assert.equal(client.streams[0].track.stopped, false);
  });
}
