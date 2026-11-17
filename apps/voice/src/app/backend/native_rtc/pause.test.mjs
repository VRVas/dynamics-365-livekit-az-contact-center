import assert from "node:assert/strict";
import test from "node:test";
import { PauseGate } from "./dist/pause.mjs";

const command = { command_id: "11111111-1111-4111-8111-111111111111", revision: 1 };
test("pause latches before mute awaits, clears pending PCM and acknowledges only after mute", async () => {
  let release, clears = 0, muted = false;
  const gate = new PauseGate(() => clears++, () => new Promise(resolve => {
    release = () => { muted = true; resolve(); };
  }));
  const pending = gate.pause(command);
  assert.equal(gate.paused, true);
  assert.equal(clears, 1);
  let acknowledged = false;
  pending.then(() => acknowledged = true);
  await Promise.resolve();
  assert.equal(acknowledged, false);
  release();
  assert.deepEqual(await pending, command);
  assert.equal(muted, true);
  assert.equal(clears, 2);
  assert.equal(await gate.pause(command), await pending);
});
test("failed mute stays latched and never returns a success acknowledgement", async () => {
  const gate = new PauseGate(() => {}, async () => { throw new Error("disconnected"); });
  await assert.rejects(gate.pause(command));
  assert.equal(gate.paused, true);
  await assert.rejects(gate.pause({ ...command, revision: 2 }));
});
