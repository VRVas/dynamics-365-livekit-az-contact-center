import { describe, it, expect, vi } from "vitest";
import { NextRequest } from "next/server";

vi.mock('@/lib/organization-knowledge', () => ({
  retrieveOrganizationKnowledge: vi.fn().mockResolvedValue({
    found: true, grounding: 'Approved technical guidance.', elapsedMs: 1,
    sources: [{ reference_id: 'test-1', title: 'Service guidance', source: 'https://docs.example.test/service' }],
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

import { POST } from "@/app/api/knowledge/search/route";

describe("POST /api/knowledge/search", () => {
  it("should accept valid search query", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/knowledge/search",
      {
        method: "POST",
        body: JSON.stringify({ query: "refund policy" }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.results).toBeDefined();
    expect(Array.isArray(data.results)).toBe(true);
  });

  it("should accept optional topK parameter", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/knowledge/search",
      {
        method: "POST",
        body: JSON.stringify({ query: "shipping", topK: 10 }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    expect(response.status).toBe(200);
  });

  it("should accept optional category parameter", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/knowledge/search",
      {
        method: "POST",
        body: JSON.stringify({ query: "billing", category: "faq" }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    expect(response.status).toBe(200);
  });

  it("should accept optional includeContent parameter", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/knowledge/search",
      {
        method: "POST",
        body: JSON.stringify({
          query: "warranty",
          includeContent: false,
        }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    expect(response.status).toBe(200);
  });

  it("should reject empty query", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/knowledge/search",
      {
        method: "POST",
        body: JSON.stringify({ query: "" }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(400);
    expect(data.error.code).toBe("VALIDATION_ERROR");
  });

  it("should reject missing query field", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/knowledge/search",
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

  it("should reject topK exceeding maximum", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/knowledge/search",
      {
        method: "POST",
        body: JSON.stringify({ query: "test", topK: 100 }),
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
      "http://localhost:3000/api/knowledge/search",
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

  it("should return totalCount in response", async () => {
    const request = new NextRequest(
      "http://localhost:3000/api/knowledge/search",
      {
        method: "POST",
        body: JSON.stringify({ query: "help" }),
        headers: { "Content-Type": "application/json" },
      }
    );

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(typeof data.totalCount).toBe("number");
  });
});
