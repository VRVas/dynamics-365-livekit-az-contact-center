import { describe, it, expect, vi, beforeAll } from "vitest";
import { NextRequest } from "next/server";

// Mock cosmos-client
vi.mock("@/lib/cosmos-client", () => ({
  listConversations: vi.fn().mockResolvedValue({
    conversations: [],
    continuationToken: undefined,
  }),
  getConversation: vi.fn().mockResolvedValue(null),
  getMessages: vi.fn().mockResolvedValue({ messages: [] }),
  saveMessage: vi.fn().mockResolvedValue({ id: "msg-001" }),
  upsertInsights: vi.fn().mockResolvedValue({}),
  getInsights: vi.fn().mockResolvedValue(null),
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

import { GET } from "@/app/api/conversations/route";

describe("GET /api/conversations", () => {
  it("should return empty list when no conversations exist", async () => {
    const request = new NextRequest("http://localhost:3000/api/conversations");

    const response = await GET(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.conversations).toBeDefined();
    expect(Array.isArray(data.conversations)).toBe(true);
  });

  it("should accept status filter parameter", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations?status=active"
    );

    const response = await GET(request);
    expect(response.status).toBe(200);
  });

  it("should accept channel filter parameter", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations?channel=chat"
    );

    const response = await GET(request);
    expect(response.status).toBe(200);
  });

  it("should accept limit parameter", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations?limit=10"
    );

    const response = await GET(request);
    expect(response.status).toBe(200);
  });

  it("should accept continuation token parameter", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations?continuationToken=abc123"
    );

    const response = await GET(request);
    expect(response.status).toBe(200);
  });

  it("should reject invalid status value", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations?status=invalid"
    );

    const response = await GET(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should reject invalid channel value", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations?channel=invalid"
    );

    const response = await GET(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should accept valid status values", async () => {
    const statuses = ["active", "closed", "on-hold"];

    for (const status of statuses) {
      const request = new NextRequest(
        `http://localhost:3000/api/conversations?status=${status}`
      );

      const response = await GET(request);
      expect(response.status).toBe(200);
    }
  });

  it("should accept valid channel values", async () => {
    const channels = ["chat", "voice", "email", "other"];

    for (const channel of channels) {
      const request = new NextRequest(
        `http://localhost:3000/api/conversations?channel=${channel}`
      );

      const response = await GET(request);
      expect(response.status).toBe(200);
    }
  });
});
