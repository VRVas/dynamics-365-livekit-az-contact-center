import { describe, it, expect, vi, beforeEach } from 'vitest';
import { NextRequest } from 'next/server';
import { GET, PATCH } from './route';
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
  getSession: vi.fn().mockImplementation((sessionId: string) => {
    if (sessionId === 'not-found') {
      return Promise.resolve(null);
    }
    return Promise.resolve({
      id: sessionId,
      sessionId,
      status: 'active',
      humanAttentionNeeded: false,
      channel: 'chat',
      customerMessageCount: 5,
      aiAgentMessageCount: 4,
      operatorMessageCount: 0,
      createdAt: '2025-12-07T00:00:00.000Z',
      updatedAt: '2025-12-07T00:05:00.000Z',
    });
  }),
  updateSession: vi.fn().mockImplementation((sessionId: string, updates: Record<string, unknown>) => {
    if (sessionId === 'not-found') {
      return Promise.resolve(null);
    }
    return Promise.resolve({
      id: sessionId,
      sessionId,
      status: updates.status || 'active',
      humanAttentionNeeded: updates.humanAttentionNeeded ?? false,
      escalationReason: updates.escalationReason,
      channel: 'chat',
      customerMessageCount: 5,
      aiAgentMessageCount: 4,
      operatorMessageCount: 0,
      createdAt: '2025-12-07T00:00:00.000Z',
      updatedAt: updates.updatedAt,
    });
  }),
}));

describe('GET /api/sessions/{sessionId}', () => {
  it('should get session by ID', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions/session-123');

    const response = await GET(request, { params: Promise.resolve({ sessionId: 'session-123' }) });
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.session).toBeDefined();
    expect(data.session.sessionId).toBe('session-123');
  });

  it('should return 404 for non-existent session', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions/not-found');

    const response = await GET(request, { params: Promise.resolve({ sessionId: 'not-found' }) });

    expect(response.status).toBe(404);
  });
});

describe('PATCH /api/sessions/{sessionId}', () => {
  it('should update session status', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions/session-123', {
      method: 'PATCH',
      body: JSON.stringify({ status: 'completed' }),
      headers: { 'Content-Type': 'application/json' },
    });

    const response = await PATCH(request, { params: Promise.resolve({ sessionId: 'session-123' }) });
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.success).toBe(true);
    expect(data.session.status).toBe('completed');
  });

  it('should update humanAttentionNeeded flag', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions/session-123', {
      method: 'PATCH',
      body: JSON.stringify({ humanAttentionNeeded: true, escalationReason: 'Customer requested human agent' }),
      headers: { 'Content-Type': 'application/json' },
    });

    const response = await PATCH(request, { params: Promise.resolve({ sessionId: 'session-123' }) });
    const data = await response.json();

    expect(response.status).toBe(200);
    expect(data.success).toBe(true);
    expect(data.session.humanAttentionNeeded).toBe(true);
    expect(data.session.escalationReason).toBe('Customer requested human agent');
  });

  it('should return 404 for non-existent session', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions/not-found', {
      method: 'PATCH',
      body: JSON.stringify({ status: 'completed' }),
      headers: { 'Content-Type': 'application/json' },
    });

    const response = await PATCH(request, { params: Promise.resolve({ sessionId: 'not-found' }) });

    expect(response.status).toBe(404);
  });

  it('should reject empty update', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions/session-123', {
      method: 'PATCH',
      body: JSON.stringify({}),
      headers: { 'Content-Type': 'application/json' },
    });

    const response = await PATCH(request, { params: Promise.resolve({ sessionId: 'session-123' }) });

    expect(response.status).toBe(400);
  });

  it('should reject invalid status', async () => {
    const request = new NextRequest('http://localhost:3000/api/sessions/session-123', {
      method: 'PATCH',
      body: JSON.stringify({ status: 'invalid' }),
      headers: { 'Content-Type': 'application/json' },
    });

    const response = await PATCH(request, { params: Promise.resolve({ sessionId: 'session-123' }) });

    expect(response.status).toBe(400);
  });
});
