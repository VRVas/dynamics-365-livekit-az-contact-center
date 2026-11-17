import { describe, it, expect, vi } from "vitest";
import { NextRequest } from "next/server";

// Mock cosmos-client
vi.mock("@/lib/cosmos-client", () => ({
  getConversation: vi.fn().mockImplementation((id: string) => 
    Promise.resolve({
      id,
      conversationId: id,
      channel: "chat",
      status: "active",
      customerId: "cust-123",
      agentId: "agent-456",
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      messageCount: 5,
    })
  ),
  listMessages: vi.fn().mockResolvedValue({ messages: [] }),
}));

// Mock NextResponse
vi.mock("next/server", async () => {
  const actual = await vi.importActual("next/server");
  return {
    ...actual,
    NextResponse: {
      json: (data: unknown, init?: ResponseInit) => {
        return {
          json: async () => data,
          status: init?.status || 200,
          _data: data,
        };
      },
    },
  };
});

import { GET } from "@/app/api/conversations/[conversationId]/route";

describe("GET /api/conversations/{conversationId}", () => {
  it("should return conversation by ID", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.conversationId).toBe("conv-123");
    expect(data.channel).toBeDefined();
    expect(data.status).toBeDefined();
    expect(data.createdAt).toBeDefined();
    expect(data.updatedAt).toBeDefined();
  });

  it("should return valid conversation structure", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/test-conv"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "test-conv" }),
    });
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(typeof data.conversationId).toBe("string");
    expect(["chat", "voice", "email", "other"]).toContain(data.channel);
    expect(["active", "closed", "on-hold"]).toContain(data.status);
    expect(data.messages).toBeDefined();
    expect(Array.isArray(data.messages)).toBe(true);
  });

  it("should accept various conversation ID formats", async () => {
    const ids = [
      "simple-id",
      "uuid-format-1234-5678-abcd",
      "numeric-123456",
      "conv_with_underscores",
    ];

    for (const id of ids) {
      const request = new NextRequest(
        `http://localhost:3000/api/conversations/${id}`
      );

      const response = await GET(request, {
        params: Promise.resolve({ conversationId: id }),
      });
      const data = await response.json();

      expect(response.status).toBe(200);
      expect(data.conversationId).toBe(id);
    }
  });
});
