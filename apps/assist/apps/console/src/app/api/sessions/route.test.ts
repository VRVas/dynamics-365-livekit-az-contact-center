import { describe, it, expect, vi, beforeEach } from 'vitest';
import { NextRequest } from 'next/server';
import { GET, POST } from './route';
import { ZodSchema } from 'zod';

// Mock api-utils
vi.mock('@/lib/api-utils', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api-utils')>(),
  apiResponse: vi.fn().mockImplementation((data, status = 200) => {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }),
  parseBody: vi.fn().mockImplementation(async (request: Request, schema: ZodSchema) => {
    try {
      const data = await request.json();
      // Actually validate with the schema
      const result = schema.safeParse(data);
      if (!result.success) {
        return { error: new Response(JSON.stringify({ error: 'Validation failed' }), { status: 400 }) };
      }
      return { data: result.data };
    } catch {
      return { error: new Response(JSON.stringify({ error: 'Invalid JSON' }), { status: 400 }) };
    }
  }),
  validateApiKey: vi.fn().mockReturnValue(true),
}));

// Mock cosmos-client
vi.mock('@/lib/cosmos-client', () => ({
  createSession: vi.fn().mockResolvedValue({
    id: 'session-123',
    sessionId: 'session-123',
    status: 'active',
    humanAttentionNeeded: false,
    channel: 'chat',
    customerMessageCount: 0,
    aiAgentMessageCount: 0,
    operatorMessageCount: 0,
    createdAt: '2025-12-07T00:00:00.000Z',
    updatedAt: '2025-12-07T00:00:00.000Z',
  }),
  listSessions: vi.fn().mockResolvedValue({
    sessions: [
      {
        id: 'session-1',
        sessionId: 'session-1',
        status: 'active',
        humanAttentionNeeded: true,
        channel: 'chat',
        customerMessageCount: 5,
        aiAgentMessageCount: 4,
        operatorMessageCount: 0,
        createdAt: '2025-12-07T00:00:00.000Z',
        updatedAt: '2025-12-07T00:05:00.000Z',
      },
      {
        id: 'session-2',
        sessionId: 'session-2',
        status: 'active',
        humanAttentionNeeded: false,
        channel: 'voice',
        customerMessageCount: 3,
        aiAgentMessageCount: 2,
        operatorMessageCount: 0,
        createdAt: '2025-12-07T00:01:00.000Z',
        updatedAt: '2025-12-07T00:04:00.000Z',
      },
    ],
    total: 2,
  }),
}));

describe('GET /api/sessions', () => {
  it('should list sessions', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions');

    const response = await GET(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.sessions).toBeDefined();
    expect(data.sessions).toHaveLength(2);
    expect(data.total).toBe(2);
  });

  it('should accept filter params', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions?status=active&humanAttentionNeeded=true');

    const response = await GET(request);

    expect(response.status).toBe(200);
  });

  it('should accept pagination params', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions?limit=10&offset=5');

    const response = await GET(request);
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.limit).toBe(10);
    expect(data.offset).toBe(5);
  });

  it('should reject invalid status', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions?status=invalid');

    const response = await GET(request);

    expect(response.status).toBe(400);
  });
});

describe('POST /api/sessions', () => {
  it('should create a new session with defaults', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions', {
      method: 'POST',
      body: JSON.stringify({}),
      headers: { 'Content-Type': 'application/json' },
    });

    const response = await POST(request);
    const data = await response.json();

    expect(response.status).toBe(201);
    expect(data.success).toBe(true);
    expect(data.session).toBeDefined();
    expect(data.session.status).toBe('active');
    expect(data.session.humanAttentionNeeded).toBe(false);
  });

  it('should create a session with custom channel', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions', {
      method: 'POST',
      body: JSON.stringify({
        channel: 'voice',
        externalSessionId: 'ext-123',
      }),
      headers: { 'Content-Type': 'application/json' },
    });

    const response = await POST(request);

    expect(response.status).toBe(201);
  });

  it('should reject invalid channel', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions', {
      method: 'POST',
      body: JSON.stringify({
        channel: 'invalid_channel',
      }),
      headers: { 'Content-Type': 'application/json' },
    });

    const response = await POST(request);

    expect(response.status).toBe(400);
  });
});
