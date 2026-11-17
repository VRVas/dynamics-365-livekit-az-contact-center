import { test } from "node:test";
import assert from "node:assert/strict";
import { PcmQueue } from "./dist/pcm.mjs";

function fixture() {
  const buffers = [], sources = [];
  const context = {
    state: "running", currentTime: 0,
    createBuffer(channels, length, rate) {
      const samples = new Float32Array(length);
      const buffer = { channels, length, rate, getChannelData: () => samples };
      buffers.push(buffer);
      return buffer;
    },
    createBufferSource() {
      const source = {
        buffer: null, onended: null, stopped: false, disconnected: false,
        connect() {}, start(time) { this.startTime = time; },
        stop() { this.stopped = true; }, disconnect() { this.disconnected = true; },
      };
      sources.push(source);
      return source;
    },
  };
  return { context, buffers, sources, queue: new PcmQueue(context, {}) };
}

test("PCM16 samples preserve sign, scale, endianness and sample rate", () => {
  const f = fixture();
  const bytes = new Uint8Array([0, 0, 255, 127, 0, 128, 0, 64]);
  f.queue.enqueue(bytes.buffer);
  assert.deepEqual(Array.from(f.buffers[0].getChannelData(0)), [0, 32767 / 32768, -1, 0.5]);
  assert.equal(f.buffers[0].channels, 1);
  assert.equal(f.buffers[0].rate, 24000);
  assert.equal(f.sources[0].startTime, 0.06);
});

test("regular 20ms frames remain ordered without accumulating delay", () => {
  const f = fixture();
  for (let index = 0; index < 100; index++) {
    f.context.currentTime = index * 0.02;
    f.queue.enqueue(new ArrayBuffer(960));
    assert.ok(Math.abs(f.sources[index].startTime - (index * 0.02 + 0.06)) < 1e-9);
    f.sources[index].onended();
    assert.equal(f.sources[index].disconnected, true);
  }
  assert.equal(f.queue.frames, 100);
});

test("500ms queue budget is enforced rather than dropping or delaying frames silently", () => {
  const f = fixture();
  for (let index = 0; index < 22; index++) f.queue.enqueue(new ArrayBuffer(960));
  assert.throws(() => f.queue.enqueue(new ArrayBuffer(960)), /500 ms/);
  assert.equal(f.queue.frames, 22);
});

test("interruption cancels queued audio and resets scheduling", () => {
  const f = fixture();
  f.queue.enqueue(new ArrayBuffer(960));
  f.queue.enqueue(new ArrayBuffer(960));
  f.queue.clear();
  assert.ok(f.sources.every(source => source.stopped && source.disconnected));
  f.context.currentTime = 4;
  f.queue.enqueue(new ArrayBuffer(960));
  assert.equal(f.sources[2].startTime, 4.06);
});

test("invalid audio and suspended output fail explicitly", () => {
  const f = fixture();
  for (const size of [0, 1, 48002]) assert.throws(() => f.queue.enqueue(new ArrayBuffer(size)));
  f.context.state = "suspended";
  assert.throws(() => f.queue.enqueue(new ArrayBuffer(960)), /not running/);
  assert.equal(f.sources.length, 0);
});
