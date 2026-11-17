import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
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

import { POST } from "@/app/api/negotiate/route";

describe("POST /api/negotiate", () => {
  beforeEach(() => {
    vi.stubEnv("SIGNALR_CONNECTION_STRING", "Endpoint=https://example.service.signalr.net;AccessKey=synthetic-test-key;Version=1.0;");
  });
  afterEach(() => vi.unstubAllEnvs());

  it("does not return mock success when unconfigured", async () => {
    vi.stubEnv("SIGNALR_CONNECTION_STRING", "");
    const response = await POST(new NextRequest("http://localhost:3000/api/negotiate", { method: "POST" }));
    expect(response.status).toBe(503);
  });

  it("should return SignalR connection info", async () => {
    const request = new NextRequest("http://localhost:3000/api/negotiate", {
      method: "POST",
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.url).toBeDefined();
    expect(data.accessToken).toBeDefined();
  });

  it("should return valid URL format", async () => {
    const request = new NextRequest("http://localhost:3000/api/negotiate", {
      method: "POST",
    });

    const response = await POST(request);
    const data = await response.json();

    expect(typeof data.url).toBe("string");
  });

  it("should return access token as string", async () => {
    const request = new NextRequest("http://localhost:3000/api/negotiate", {
      method: "POST",
    });

    const response = await POST(request);
    const data = await response.json();

    expect(typeof data.accessToken).toBe("string");
  });
});
