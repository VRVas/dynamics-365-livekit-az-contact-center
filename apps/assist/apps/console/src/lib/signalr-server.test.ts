import crypto from "crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const endpoint = "https://example.service.signalr.net";
const key = "synthetic-test-key";

function signed(payload: Record<string, unknown>) {
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  const signature = crypto.createHmac("sha256", key).update(`${header}.${body}`).digest("base64url");
  return `${header}.${body}.${signature}`;
}

describe("serverless SignalR REST integration", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("SIGNALR_CONNECTION_STRING", `Endpoint=${endpoint};AccessKey=${key};Version=1.0;`);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 202 }));
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("uses versioned REST endpoints and request-specific token audiences", async () => {
    const signalr = await import("./signalr-server");
    await signalr.sendToGroup("conversation", "newMessage", { value: "sample" });
    await signalr.broadcast("conversationUpdated", {});
    await signalr.sendToUser("subscriber", "notification", {});
    const calls = vi.mocked(fetch).mock.calls;
    expect(calls.map(call => call[0])).toEqual([
      `${endpoint}/api/v1/hubs/agentassist/groups/conversation`,
      `${endpoint}/api/v1/hubs/agentassist`,
      `${endpoint}/api/v1/hubs/agentassist/users/subscriber`,
    ]);
    for (const [url, options] of calls) {
      const token = new Headers(options?.headers).get("Authorization")!.slice(7);
      const claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
      expect(claims.aud).toBe(url);
    }
  });

  it("authorizes only signed, unexpired client tokens for group membership", async () => {
    const signalr = await import("./signalr-server");
    const connection = signalr.negotiate("subscriber");
    expect(signalr.subscriberFromToken(connection.accessToken)).toBe("subscriber");
    const payload = { aud: connection.url, exp: Math.floor(Date.now() / 1000) + 60, nameid: "subscriber" };
    expect(signalr.subscriberFromToken(signed({ ...payload, exp: 1 }))).toBeNull();
    expect(signalr.subscriberFromToken(signed({ ...payload, aud: endpoint + "/api/v1/hubs/agentassist" }))).toBeNull();
    expect(signalr.subscriberFromToken(connection.accessToken + "tampered")).toBeNull();
    expect(signalr.subscriberFromToken("not-a-token")).toBeNull();
  });

  it("joins a bounded-lifetime user group through REST, never a hub invocation", async () => {
    const signalr = await import("./signalr-server");
    await signalr.setConversationMembership("subscriber", "conversation", true);
    const [url, options] = vi.mocked(fetch).mock.calls[0];
    expect(url).toBe(`${endpoint}/api/v1/hubs/agentassist/groups/conversation/users/subscriber?ttl=3600`);
    expect(options?.method).toBe("PUT");
    const token = new Headers(options?.headers).get("Authorization")!.slice(7);
    const claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
    expect(claims.aud).toBe(String(url).split("?")[0]);
  });
});
