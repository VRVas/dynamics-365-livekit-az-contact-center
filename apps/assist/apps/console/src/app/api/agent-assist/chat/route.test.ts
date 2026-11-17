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
      { id: "msg-1", content: "Hello", sender: "customer", timestamp: new Date().toISOString() },
    ],
  }),
}));

// Mock openai-client
vi.mock("@/lib/openai-client", () => ({
  agentAssistChat: vi.fn().mockResolvedValue({
    message: "Our refund policy allows returns within 30 days of purchase.",
    sources: [
      { title: "Refund Policy", url: "https://example.com/refund" },
    ],
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

import { POST } from "@/app/api/agent-assist/chat/route";

describe("POST /api/agent-assist/chat", () => {
  it("should accept valid question", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/agent-assist/chat",
      {
        method: "POST",
        body: JSON.stringify({
          question: "What is the refund policy?",
        }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.answer).toBeDefined();
    expect(typeof data.answer).toBe("string");
  });

  it("should return citations array", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/agent-assist/chat",
      {
        method: "POST",
        body: JSON.stringify({
          question: "How do I process a return?",
        }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.citations).toBeDefined();
    expect(Array.isArray(data.citations)).toBe(true);
  });

  it("should return confidence score", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/agent-assist/chat",
      {
        method: "POST",
        body: JSON.stringify({
          question: "What are the shipping options?",
        }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(typeof data.confidence).toBe("number");
    expect(data.confidence).toBeGreaterThanOrEqual(0);
    expect(data.confidence).toBeLessThanOrEqual(1);
  });

  it("should accept optional conversationId", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/agent-assist/chat",
      {
        method: "POST",
        body: JSON.stringify({
          question: "Can you help with this order?",
          conversationId: "conv-123",
        }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    expect(response.status).toBe(200);
  });

  it("should accept optional conversationContext", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/agent-assist/chat",
      {
        method: "POST",
        body: JSON.stringify({
          question: "What should I do next?",
          conversationContext:
            "Customer is asking about a delayed shipment for order #12345",
        }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    expect(response.status).toBe(200);
  });

  it("should reject empty question", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/agent-assist/chat",
      {
        method: "POST",
        body: JSON.stringify({ question: "" }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should reject missing question field", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/agent-assist/chat",
      {
        method: "POST",
        body: JSON.stringify({}),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should reject invalid JSON", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/agent-assist/chat",
      {
        method: "POST",
        body: "not json",
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("INVALID_JSON");
  });

  it("should handle long questions", async () => {
    const longQuestion = "What is the policy for ".repeat(100) + "refunds?";

    const request = new NextRequest(
      "http://localhost:3000/api/agent-assist/chat",
      {
        method: "POST",
        body: JSON.stringify({ question: longQuestion }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    expect(response.status).toBe(200);
  });
});
