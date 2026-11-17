import { describe, it, expect, beforeEach, vi } from "vitest";
import { NextRequest } from "next/server";

// Mock cosmos-client
vi.mock("@/lib/cosmos-client", () => ({
  // Session-based functions (new)
  getSession: vi.fn().mockResolvedValue({
    id: "conv-123",
    sessionId: "conv-123",
    channel: "chat",
    status: "active",
    humanAttentionNeeded: false,
    messageCount: 0,
    customerMessageCount: 0,
    aiAgentMessageCount: 0,
    operatorMessageCount: 0,
  }),
  ensureSessionExists: vi.fn().mockImplementation((id: string, channel: string = 'chat') => 
    Promise.resolve({
      id,
      sessionId: id,
      channel,
      status: "active",
      humanAttentionNeeded: false,
      messageCount: 0,
      customerMessageCount: 0,
      aiAgentMessageCount: 0,
      operatorMessageCount: 0,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    })
  ),
  // Legacy compatibility aliases
  getConversation: vi.fn().mockResolvedValue({
    id: "conv-123",
    conversationId: "conv-123",
    channel: "chat",
    status: "active",
  }),
  ensureConversationExists: vi.fn().mockImplementation((id: string) => 
    Promise.resolve({
      id,
      conversationId: id,
      channel: "chat",
      status: "active",
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    })
  ),
  createMessage: vi.fn().mockImplementation((msg: { id: string; sessionId: string; sender: string; content: string }) => 
    Promise.resolve({
      id: msg.id,
      sessionId: msg.sessionId,
      sender: msg.sender,
      content: msg.content,
      timestamp: new Date().toISOString(),
    })
  ),
  saveMessage: vi.fn().mockImplementation((msg: { messageId: string; conversationId: string }) => 
    Promise.resolve({
      id: msg.messageId,
      messageId: msg.messageId,
      conversationId: msg.conversationId,
    })
  ),
  createConversation: vi.fn().mockResolvedValue({
    id: "conv-123",
    conversationId: "conv-123",
    channel: "chat",
    status: "active",
  }),
  listMessages: vi.fn().mockResolvedValue({ messages: [] }),
  upsertInsights: vi.fn().mockResolvedValue(undefined),
}));

// Mock signalr-server
vi.mock("@/lib/signalr-server", () => ({
  broadcastMessage: vi.fn().mockResolvedValue(undefined),
  notifyNewMessage: vi.fn().mockResolvedValue(undefined),
  notifyConversationUpdate: vi.fn().mockResolvedValue(undefined),
  notifyInsightsUpdated: vi.fn().mockResolvedValue(undefined),
}));

// Mock openai-client with comprehensive insights structure
vi.mock("@/lib/openai-client", () => ({
  generateInsights: vi.fn().mockResolvedValue({
    sentiment: {
      current: { label: "neutral", score: 0, confidence: 0.5 },
      history: [],
      frustrationScore: 0,
    },
    customerKPIs: {
      issueComplexity: "moderate",
      escalationRisk: "low",
      churnRisk: "low",
    },
    conversationKPIs: {
      durationSeconds: 0,
      estimatedHandleTimeSeconds: 300,
      customerMessageCount: 1,
      agentMessageCount: 0,
      firstTimeResolutionLikelihood: 0.5,
      topicIntent: "General Inquiry",
    },
    complianceKPIs: {
      greetingDetected: false,
      identityVerified: false,
      disclaimerMentioned: false,
    },
    nextBestActions: [],
    summary: "Conversation in progress.",
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

import { POST } from "@/app/api/ingest/message/route";

describe("POST /api/ingest/message", () => {
  beforeEach(() => {
    vi.resetModules();
    // Set development mode to bypass API key check
    vi.stubEnv('NODE_ENV', 'development');
  });

  it("should accept valid message payload", async () => {
    const validPayload = {
      conversation_id: "conv-123",
      message_id: "msg-001",
      timestamp: "2025-12-03T15:00:00.000Z",
      speaker: "customer",
      channel: "chat",
      text: "Hello, I need help with my order",
    };

    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: JSON.stringify(validPayload),
      headers: { "Content-Type": "application/json" },
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(201);
    expect(data.success).toBe(true);
    expect(data.messageId).toBe("msg-001");
    expect(data.conversationId).toBe("conv-123");
  });

  it("should accept message with optional metadata", async () => {
    const payloadWithMetadata = {
      conversation_id: "conv-456",
      message_id: "msg-002",
      timestamp: "2025-12-03T15:01:00.000Z",
      speaker: "ai_agent",
      channel: "voice",
      text: "How can I help you today?",
      metadata: {
        language: "en",
        source_system: "genesys",
        transcription_confidence: 0.95,
        tags: ["greeting", "support"],
      },
    };

    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: JSON.stringify(payloadWithMetadata),
      headers: { "Content-Type": "application/json" },
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(201);
    expect(data.success).toBe(true);
  });

  it("should reject invalid speaker type", async () => {
    const invalidPayload = {
      conversation_id: "conv-123",
      message_id: "msg-001",
      timestamp: "2025-12-03T15:00:00.000Z",
      speaker: "invalid_speaker",
      channel: "chat",
      text: "Hello",
    };

    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: JSON.stringify(invalidPayload),
      headers: { "Content-Type": "application/json" },
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should reject invalid channel type", async () => {
    const invalidPayload = {
      conversation_id: "conv-123",
      message_id: "msg-001",
      timestamp: "2025-12-03T15:00:00.000Z",
      speaker: "customer",
      channel: "invalid_channel",
      text: "Hello",
    };

    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: JSON.stringify(invalidPayload),
      headers: { "Content-Type": "application/json" },
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should reject missing required fields", async () => {
    const incompletePayload = {
      conversation_id: "conv-123",
      // missing message_id, timestamp, speaker, channel, text
    };

    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: JSON.stringify(incompletePayload),
      headers: { "Content-Type": "application/json" },
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
    expect(data.error.details).toBeDefined();
    expect(data.error.details.length).toBeGreaterThan(0);
  });

  it("should reject empty text", async () => {
    const emptyTextPayload = {
      conversation_id: "conv-123",
      message_id: "msg-001",
      timestamp: "2025-12-03T15:00:00.000Z",
      speaker: "customer",
      channel: "chat",
      text: "",
    };

    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: JSON.stringify(emptyTextPayload),
      headers: { "Content-Type": "application/json" },
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should reject invalid JSON", async () => {
    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: "not valid json",
      headers: { "Content-Type": "application/json" },
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("INVALID_JSON");
  });

  it("should reject invalid timestamp format", async () => {
    const invalidTimestamp = {
      conversation_id: "conv-123",
      message_id: "msg-001",
      timestamp: "not-a-valid-timestamp",
      speaker: "customer",
      channel: "chat",
      text: "Hello",
    };

    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: JSON.stringify(invalidTimestamp),
      headers: { "Content-Type": "application/json" },
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should accept all valid speaker types", async () => {
    const speakers = ["customer", "ai_agent", "operator"];

    for (const speaker of speakers) {
      const payload = {
        conversation_id: "conv-123",
        message_id: `msg-${speaker}`,
        timestamp: "2025-12-03T15:00:00.000Z",
        speaker,
        channel: "chat",
        text: `Message from ${speaker}`,
      };

      const request = new NextRequest("http://localhost:3000/api/ingest/message", {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      });

      const response = await POST(request);
      expect(response.status).toBe(201);
    }
  });

  it("should accept all valid channel types", async () => {
    const channels = ["chat", "voice", "email", "other"];

    for (const channel of channels) {
      const payload = {
        conversation_id: "conv-123",
        message_id: `msg-${channel}`,
        timestamp: "2025-12-03T15:00:00.000Z",
        speaker: "customer",
        channel,
        text: `Message via ${channel}`,
      };

      const request = new NextRequest("http://localhost:3000/api/ingest/message", {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      });

      const response = await POST(request);
      expect(response.status).toBe(201);
    }
  });
});

describe("POST /api/ingest/message - Authentication", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("should require API key in production", async () => {
    vi.stubEnv('NODE_ENV', 'production');
    vi.stubEnv('API_KEY', 'test-api-key-12345');

    const validPayload = {
      conversation_id: "conv-123",
      message_id: "msg-001",
      timestamp: "2025-12-03T15:00:00.000Z",
      speaker: "customer",
      channel: "chat",
      text: "Hello",
    };

    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: JSON.stringify(validPayload),
      headers: { "Content-Type": "application/json" },
    });

    // Re-import to get fresh module with new env
    const { POST: postHandler } = await import("@/app/api/ingest/message/route");
    const response = await postHandler(request);
    const data = await response.json();

    expect(response.status).toBe(401);
    expect(data.error.code).toBe("UNAUTHORIZED");
  });

  it("should accept valid API key in production", async () => {
    vi.stubEnv('NODE_ENV', 'production');
    vi.stubEnv('API_KEY', 'test-api-key-12345');

    const validPayload = {
      conversation_id: "conv-123",
      message_id: "msg-001",
      timestamp: "2025-12-03T15:00:00.000Z",
      speaker: "customer",
      channel: "chat",
      text: "Hello",
    };

    const request = new NextRequest("http://localhost:3000/api/ingest/message", {
      method: "POST",
      body: JSON.stringify(validPayload),
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": "test-api-key-12345",
      },
    });

    const { POST: postHandler } = await import("@/app/api/ingest/message/route");
    const response = await postHandler(request);

    expect(response.status).toBe(201);
  });
});
