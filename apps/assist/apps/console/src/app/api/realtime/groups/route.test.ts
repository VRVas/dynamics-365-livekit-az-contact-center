import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

vi.mock("@/lib/signalr-server", () => ({
  subscriberFromToken: vi.fn(),
  setConversationMembership: vi.fn(),
}));
import { subscriberFromToken, setConversationMembership } from "@/lib/signalr-server";
import { POST } from "./route";

describe("POST /api/realtime/groups", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubEnv("SIGNALR_CONNECTION_STRING", "synthetic");
    vi.mocked(subscriberFromToken).mockReturnValue("server-issued-subscriber");
    vi.mocked(setConversationMembership).mockResolvedValue();
  });
  afterEach(() => vi.unstubAllEnvs());

  function request(body: unknown, token = "signed.token.value") {
    return new NextRequest("http://localhost:3000/api/realtime/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    });
  }

  it("takes subscriber identity from the verified token, not request data", async () => {
    const response = await POST(request({ conversationId: "sample", action: "join" }));
    expect(response.status).toBe(200);
    expect(setConversationMembership).toHaveBeenCalledWith("server-issued-subscriber", "sample", true);
  });

  it("rejects invalid tokens without changing subscriptions", async () => {
    vi.mocked(subscriberFromToken).mockReturnValue(null);
    expect((await POST(request({ conversationId: "sample", action: "join" }))).status).toBe(401);
    expect(setConversationMembership).not.toHaveBeenCalled();
  });

  it("rejects attempts to supply another subscriber identity", async () => {
    const response = await POST(request({ conversationId: "sample", action: "join", userId: "someone-else" }));
    expect(response.status).toBe(400);
    expect(setConversationMembership).not.toHaveBeenCalled();
  });
});
