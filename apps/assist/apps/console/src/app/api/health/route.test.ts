import { afterEach, describe, it, expect, vi } from "vitest";
import { NextRequest } from "next/server";

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

import { GET } from "@/app/api/health/route";

describe("GET /api/health", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("reports configuration without claiming dependency connectivity", async () => {
    vi.stubEnv("COSMOS_DB_ENDPOINT", "https://example.documents.azure.com");
    vi.stubEnv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com");
    vi.stubEnv("SIGNALR_CONNECTION_STRING", "synthetic-connection");
    const response = await GET(new NextRequest("http://localhost:3000/api/health"));
    const data = await response.json();
    expect(data.check).toBe("process-and-configuration");
    expect(data.services.cosmosDb).toBe("configured");
    expect(data.services.openAi).toBe("configured");
    expect(data.services.signalR).toBe("configured");
  });

  it("should return health status", async () => {
    const request = new NextRequest("http://localhost:3000/api/health");

    const response = await GET(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.status).toBe("healthy");
  });

  it("should return timestamp", async () => {
    const request = new NextRequest("http://localhost:3000/api/health");

    const response = await GET(request);
    const data = await response.json();

    expect(data.timestamp).toBeDefined();
    expect(new Date(data.timestamp).toISOString()).toBe(data.timestamp);
  });

  it("should return version", async () => {
    const request = new NextRequest("http://localhost:3000/api/health");

    const response = await GET(request);
    const data = await response.json();

    expect(data.version).toBeDefined();
    expect(typeof data.version).toBe("string");
  });

  it("should return services status", async () => {
    const request = new NextRequest("http://localhost:3000/api/health");

    const response = await GET(request);
    const data = await response.json();

    expect(data.services).toBeDefined();
    expect(data.services.cosmosDb).toBeDefined();
    expect(data.services.openAi).toBeDefined();
    expect(data.services.signalR).toBeDefined();
    expect(data.services.blobStorage).toBeDefined();
  });
});
