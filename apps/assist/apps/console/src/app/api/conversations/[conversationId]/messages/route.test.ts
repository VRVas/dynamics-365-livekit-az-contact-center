import { describe, it, expect, vi } from "vitest";
import { NextRequest } from "next/server";

// Mock cosmos-client
vi.mock("@/lib/cosmos-client", () => ({
  getConversation: vi.fn().mockResolvedValue({
    id: "conv-123",
    conversationId: "conv-123",
    channel: "chat",
    status: "active",
  }),
  listMessages: vi.fn().mockResolvedValue({
    messages: [
      {
        id: "msg-001",
        conversationId: "conv-123",
        content: "Hello",
        sender: "customer",
        timestamp: new Date().toISOString(),
      },
    ],
    hasMore: false,
  }),
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

import { GET } from "@/app/api/conversations/[conversationId]/messages/route";

describe("GET /api/conversations/{conversationId}/messages", () => {
  it("should return messages for a conversation", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/messages"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.messages).toBeDefined();
    expect(Array.isArray(data.messages)).toBe(true);
    expect(typeof data.hasMore).toBe("boolean");
  });

  it("should accept limit parameter", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/messages?limit=10"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });

    expect(response.status).toBe(200);
  });

  it("should accept before timestamp parameter", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/messages?before=2025-12-03T15:00:00.000Z"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });

    expect(response.status).toBe(200);
  });

  it("should reject invalid before timestamp", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/messages?before=not-a-timestamp"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should handle combined pagination parameters", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/messages?limit=25&before=2025-12-03T15:00:00.000Z"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });

    expect(response.status).toBe(200);
  });
});
