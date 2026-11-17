const test = require("node:test");
const assert = require("node:assert/strict");
const { PauseControl, text } = require("../dynamics/native-supervision.js");
const conversation = "11111111-1111-4111-8111-111111111111";
const command = "22222222-2222-4222-8222-222222222222";
function fixture() {
  const shown = [], calls = [], storage = new Map();
  let focused = "native-session";
  const host = {
    Microsoft: {
      Apm: { getFocusedSession: () => ({ sessionId: focused, isDefault: false }) },
      Omnichannel: { getConversationId: async () => conversation }
    },
    Xrm: { WebApi: { online: {
      createRecord: async (table, data) => { calls.push(data); return { id: command }; },
      retrieveRecord: async () => ({
        moacc_nativeaicommandid: command, moacc_conversationid: conversation, moacc_action: "pause",
        moacc_outcome: "paused", moacc_ackrevision: 1, moacc_ackon: "2026-01-01T00:00:00Z"
      })
    } } }
  };
  const control = new PauseControl(host, {
    show: state => shown.push(state), uuid: () => command, sleep: async () => {},
    storage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) }
  });
  return { host, control, shown, calls, focus: value => focused = value };
}
test("monitoring/creating a control never submits; explicit click requires a real correlated receipt", async () => {
  const f = fixture();
  assert.equal(f.calls.length, 0);
  await f.control.request();
  assert.deepEqual(f.shown, ["requested", "paused"]);
  assert.deepEqual(Object.keys(f.calls[0]).sort(), ["moacc_action", "moacc_conversationid", "moacc_nativeaicommandid"]);
  await f.control.request();
  assert.equal(f.calls.length, 1);
});
test("focus change during native ID lookup prevents command creation", async () => {
  const f = fixture();
  f.host.Microsoft.Omnichannel.getConversationId = async () => { f.focus("another"); return conversation; };
  await f.control.request();
  assert.deepEqual(f.shown, ["changed"]);
  assert.equal(f.calls.length, 0);
});
test("native GUID formatting is canonicalized without selecting a different conversation", async () => {
  const f = fixture();
  f.host.Microsoft.Omnichannel.getConversationId = async () => "{" + conversation.toUpperCase() + "}";
  await f.control.request();
  assert.equal(f.calls[0].moacc_conversationid, conversation);
  assert.equal(f.shown.at(-1), "paused");
});
test("creation and a missing acknowledgement cannot look like success", async () => {
  const f = fixture();
  f.host.Xrm.WebApi.online.retrieveRecord = async () => ({
    moacc_nativeaicommandid: command, moacc_conversationid: conversation, moacc_action: "pause",
    moacc_outcome: "paused"
  });
  await f.control.request();
  assert.deepEqual(f.shown, ["requested", "unconfirmed"]);
});
test("pending polling is bounded, wrong conversations are rejected and both languages exist", async () => {
  const f = fixture();
  let reads = 0;
  f.host.Xrm.WebApi.online.retrieveRecord = async () => {
    reads++;
    return { moacc_nativeaicommandid: command, moacc_conversationid: conversation, moacc_action: "pause" };
  };
  await f.control.request();
  assert.equal(reads, 45);
  assert.equal(f.shown.at(-1), "unconfirmed");
  f.host.Xrm.WebApi.online.retrieveRecord = async () => ({ moacc_nativeaicommandid: command, moacc_conversationid: command });
  await f.control.request();
  assert.equal(f.shown.at(-1), "failed");
  assert.equal(Object.keys(text.en).join(), Object.keys(text.ar).join());
});
test("the poll budget is wall-clock bounded, not just a count of slow requests", async () => {
  const f = fixture();
  let now = 0;
  f.control.now = () => now;
  const read = f.host.Xrm.WebApi.online.retrieveRecord;
  f.host.Xrm.WebApi.online.retrieveRecord = async () => { now = 46000; return read(); };
  await f.control.request();
  assert.equal(f.shown.at(-1), "unconfirmed");
  assert.equal(f.shown.includes("paused"), false);
});

function pausedReceipt(data) {
  return { ...data, moacc_outcome: "paused", moacc_ackrevision: 1, moacc_ackon: "2026-01-01T00:00:00Z" };
}

test("a create that never reached Dataverse can recover after reload using the cached UUID", async () => {
  const f = fixture();
  let online = false, stored, rowsCreated = 0;
  f.host.Xrm.WebApi.online.createRecord = async (_table, data) => {
    f.calls.push(data);
    if (!online) throw new Error("network unavailable");
    stored = pausedReceipt(data);
    rowsCreated++;
    return { id: command };
  };
  f.host.Xrm.WebApi.online.retrieveRecord = async () => {
    if (!online) throw new Error("network unavailable");
    if (!stored) throw { errorCode: 0x80040217 };
    return stored;
  };
  await f.control.request();
  assert.equal(f.shown.at(-1), "unconfirmed");
  online = true;
  const reloaded = new PauseControl(f.host, {
    storage: f.control.storage, show: value => f.shown.push(value), sleep: async () => {},
    uuid: () => assert.fail("The original UUID must be reused")
  });
  await reloaded.request();
  assert.equal(f.calls.length, 2);
  assert.equal(rowsCreated, 1);
  assert.deepEqual(f.calls.map(data => data.moacc_nativeaicommandid), [command, command]);
  assert.equal(f.shown.at(-1), "paused");
  await reloaded.request();
  assert.equal(f.calls.length, 2);
});

test("confirmed HTTP and Dataverse record-not-found results allow only a same-ID create retry", async () => {
  for (const missing of [{ status: 404 }, { statusCode: 404 }, { errorCode: 0x80040217 }, { errorCode: -2147220969 }]) {
    const f = fixture();
    let stored;
    f.host.Xrm.WebApi.online.createRecord = async (_table, data) => {
      f.calls.push(data);
      if (f.calls.length === 1) throw new Error("first create did not arrive");
      stored = pausedReceipt(data);
      return { id: command };
    };
    f.host.Xrm.WebApi.online.retrieveRecord = async () => {
      if (!stored) throw missing;
      return stored;
    };
    await f.control.request();
    assert.equal(f.calls.length, 1);
    assert.equal(f.shown.at(-1), "unconfirmed");
    await f.control.request();
    assert.deepEqual(f.calls.map(data => data.moacc_nativeaicommandid), [command, command]);
    assert.equal(f.shown.at(-1), "paused");
  }
});

test("unknown network, timeout and denied reads never authorize a cached create retry", async () => {
  for (const failure of [new Error("timeout"), { status: 403 }, { status: 500, message: "404 record not found" }, { errorCode: 0x80040220 }]) {
    const f = fixture();
    f.host.Xrm.WebApi.online.createRecord = async (_table, data) => {
      f.calls.push(data);
      throw new Error("unknown create result");
    };
    f.host.Xrm.WebApi.online.retrieveRecord = async () => { throw failure; };
    await f.control.request();
    await f.control.request();
    assert.equal(f.calls.length, 1);
    assert.equal(f.shown.at(-1), "unconfirmed");
    assert.equal(f.shown.includes("paused"), false);
  }
});

test("focus changing while a not-found read completes prevents the same-ID create retry", async () => {
  const f = fixture();
  f.host.Xrm.WebApi.online.createRecord = async (_table, data) => {
    f.calls.push(data);
    throw new Error("create failed before reaching server");
  };
  f.host.Xrm.WebApi.online.retrieveRecord = async () => { throw new Error("offline"); };
  await f.control.request();
  f.host.Xrm.WebApi.online.retrieveRecord = async () => {
    f.focus("another-conversation");
    throw { status: 404 };
  };
  await f.control.request();
  assert.equal(f.calls.length, 1);
  assert.equal(f.shown.at(-1), "changed");
});

test("an ambiguous successful first create is read back without creating another command", async () => {
  const f = fixture();
  let stored;
  f.host.Xrm.WebApi.online.createRecord = async (_table, data) => {
    f.calls.push(data);
    stored = pausedReceipt(data);
    throw new Error("successful create response was lost");
  };
  f.host.Xrm.WebApi.online.retrieveRecord = async () => stored;
  await f.control.request();
  await f.control.request();
  assert.equal(f.calls.length, 1);
  assert.equal(f.shown.at(-1), "paused");
});

test("a duplicate response after a not-found retry reconciles one existing row with the same UUID", async () => {
  const f = fixture();
  let stored, reads = 0, rowsCreated = 0;
  f.host.Xrm.WebApi.online.createRecord = async (_table, data) => {
    f.calls.push(data);
    if (stored) throw { status: 409 };
    stored = pausedReceipt(data);
    rowsCreated++;
    throw new Error("first response lost");
  };
  f.host.Xrm.WebApi.online.retrieveRecord = async () => {
    if (++reads === 1) throw new Error("read connection lost");
    if (reads === 2) throw { errorCode: 0x80040217 };
    return stored;
  };
  await f.control.request();
  assert.equal(f.shown.at(-1), "unconfirmed");
  await f.control.request();
  assert.equal(f.calls.length, 2);
  assert.equal(rowsCreated, 1);
  assert.deepEqual(f.calls.map(data => data.moacc_nativeaicommandid), [command, command]);
  assert.equal(f.shown.at(-1), "paused");
});

test("repeated not-found responses cannot cause an unbounded create loop in one click", async () => {
  const f = fixture();
  f.host.Xrm.WebApi.online.createRecord = async (_table, data) => {
    f.calls.push(data);
    throw new Error("offline write");
  };
  f.host.Xrm.WebApi.online.retrieveRecord = async () => { throw { status: 404 }; };
  await f.control.request();
  await f.control.request();
  assert.equal(f.calls.length, 2);
  assert.deepEqual(f.calls.map(data => data.moacc_nativeaicommandid), [command, command]);
  assert.equal(f.shown.at(-1), "unconfirmed");
});
