import { describe, it, expect, vi } from "vitest";
import { NextRequest } from "next/server";

// Mock cosmos-client with comprehensive insights structure
vi.mock("@/lib/cosmos-client", () => ({
  getConversation: vi.fn().mockResolvedValue({
    id: "conv-123",
    conversationId: "conv-123",
    channel: "chat",
    status: "active",
    customerId: "cust-123",
    agentId: "agent-456",
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    messageCount: 5,
  }),
  getInsights: vi.fn().mockResolvedValue({
    conversationId: "conv-123",
    updatedAt: new Date().toISOString(),
    sentiment: {
      current: { label: "positive", score: 0.8, confidence: 0.9 },
      history: [],
      frustrationScore: 0.1,
    },
    customerKPIs: {
      issueComplexity: "simple",
      escalationRisk: "low",
      churnRisk: "low",
    },
    conversationKPIs: {
      durationSeconds: 120,
      estimatedHandleTimeSeconds: 300,
      customerMessageCount: 3,
      agentMessageCount: 2,
      firstTimeResolutionLikelihood: 0.85,
      topicIntent: "Billing > Account Inquiry",
    },
    complianceKPIs: {
      greetingDetected: true,
      identityVerified: false,
      disclaimerMentioned: false,
    },
    nextBestActions: [
      {
        id: "nba-1",
        type: "reply_template",
        label: "Check account status",
        description: "Review customer's current account details",
        priority: 1,
      },
    ],
    summary: "Customer inquiring about billing issues",
    extractedEntities: {
      customerName: "John Doe",
      productsMentioned: ["Premium Plan"],
      orderNumbers: [],
      ticketNumbers: [],
    },
  }),
  listMessages: vi.fn().mockResolvedValue({
    messages: [
      { id: "msg-1", content: "Hello", sender: "customer", timestamp: new Date().toISOString() },
    ],
  }),
  upsertInsights: vi.fn().mockResolvedValue({}),
}));

// Mock openai-client with comprehensive insights structure
vi.mock("@/lib/openai-client", () => ({
  generateInsights: vi.fn().mockResolvedValue({
    sentiment: {
      current: { label: "positive", score: 0.8, confidence: 0.9 },
      history: [],
      frustrationScore: 0.1,
    },
    customerKPIs: {
      issueComplexity: "simple",
      escalationRisk: "low",
      churnRisk: "low",
    },
    conversationKPIs: {
      durationSeconds: 120,
      estimatedHandleTimeSeconds: 300,
      customerMessageCount: 3,
      agentMessageCount: 2,
      firstTimeResolutionLikelihood: 0.85,
      topicIntent: "Billing > Account Inquiry",
    },
    complianceKPIs: {
      greetingDetected: true,
      identityVerified: false,
      disclaimerMentioned: false,
    },
    nextBestActions: [
      {
        id: "nba-1",
        type: "reply_template",
        label: "Check account status",
        description: "Review customer's current account details",
        priority: 1,
      },
    ],
    summary: "Customer inquiry about billing",
    extractedEntities: {
      productsMentioned: [],
      orderNumbers: [],
      ticketNumbers: [],
    },
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

import { GET } from "@/app/api/conversations/[conversationId]/insights/route";

describe("GET /api/conversations/{conversationId}/insights", () => {
  it("should return insights for a conversation", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/insights"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.conversationId).toBe("conv-123");
  });

  it("should return valid sentiment structure", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/insights"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(data.sentiment).toBeDefined();
    expect(data.sentiment.current).toBeDefined();
    expect(data.sentiment.current.label).toBeDefined();
    expect(["positive", "neutral", "negative"]).toContain(
      data.sentiment.current.label
    );
    expect(typeof data.sentiment.current.score).toBe("number");
    expect(typeof data.sentiment.current.confidence).toBe("number");
    expect(Array.isArray(data.sentiment.history)).toBe(true);
    expect(typeof data.sentiment.frustrationScore).toBe("number");
  });

  it("should return valid customer KPIs structure", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/insights"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(data.customerKPIs).toBeDefined();
    expect(["simple", "moderate", "complex"]).toContain(
      data.customerKPIs.issueComplexity
    );
    expect(["low", "medium", "high"]).toContain(
      data.customerKPIs.escalationRisk
    );
    expect(["low", "medium", "high"]).toContain(data.customerKPIs.churnRisk);
  });

  it("should return valid conversation KPIs structure", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/insights"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(data.conversationKPIs).toBeDefined();
    expect(typeof data.conversationKPIs.durationSeconds).toBe("number");
    expect(typeof data.conversationKPIs.customerMessageCount).toBe("number");
    expect(typeof data.conversationKPIs.agentMessageCount).toBe("number");
  });

  it("should return valid compliance KPIs structure", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/insights"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(data.complianceKPIs).toBeDefined();
    expect(typeof data.complianceKPIs.greetingDetected).toBe("boolean");
    expect(typeof data.complianceKPIs.identityVerified).toBe("boolean");
    expect(typeof data.complianceKPIs.disclaimerMentioned).toBe("boolean");
  });

  it("should return next best actions array", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/insights"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(data.nextBestActions).toBeDefined();
    expect(Array.isArray(data.nextBestActions)).toBe(true);
  });

  it("should return summary and extracted entities", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/insights"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(typeof data.summary).toBe("string");
    expect(data.extractedEntities).toBeDefined();
    expect(Array.isArray(data.extractedEntities.productsMentioned)).toBe(true);
    expect(Array.isArray(data.extractedEntities.orderNumbers)).toBe(true);
    expect(Array.isArray(data.extractedEntities.ticketNumbers)).toBe(true);
  });

  it("should return valid updatedAt timestamp", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/conversations/conv-123/insights"
    );

    const response = await GET(request, {
      params: Promise.resolve({ conversationId: "conv-123" }),
    });
    const data = await response.json();

    expect(data.updatedAt).toBeDefined();
    expect(new Date(data.updatedAt).toISOString()).toBe(data.updatedAt);
  });
});
