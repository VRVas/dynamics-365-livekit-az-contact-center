import { NextRequest, NextResponse } from 'next/server';
import { z } from 'zod';
import { apiResponse, parseBody, validateApiKey, moduleUnavailable } from '@/lib/api-utils';
import { createSession, listSessions } from '@/lib/cosmos-client';

// Session list query params schema
const SessionListQuerySchema = z.object({
  status: z.enum(['active', 'completed', 'abandoned']).optional(),
  humanAttentionNeeded: z.enum(['true', 'false']).optional(),
  limit: z.string().regex(/^\d+$/).transform(Number).optional(),
  offset: z.string().regex(/^\d+$/).transform(Number).optional(),
});

// Create session request schema
const CreateSessionRequestSchema = z.object({
  externalSessionId: z.string().optional(),
  channel: z.enum(['chat', 'voice', 'email', 'other']).default('chat'),
  metadata: z.record(z.string(), z.unknown()).optional(),
});

/**
 * GET /api/sessions
 * List sessions with filtering and priority sorting
 * Priority: humanAttentionNeeded+active > active > completed > abandoned
 */
export async function GET(request: NextRequest) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  try {
    const { searchParams } = new URL(request.url);
    
    // Parse query params
    const queryResult = SessionListQuerySchema.safeParse({
      status: searchParams.get('status') ?? undefined,
      humanAttentionNeeded: searchParams.get('humanAttentionNeeded') ?? undefined,
      limit: searchParams.get('limit') ?? undefined,
      offset: searchParams.get('offset') ?? undefined,
    });

    if (!queryResult.success) {
      return apiResponse(
        { error: 'Invalid query parameters', details: queryResult.error.format() },
        400
      );
    }

    const { status, humanAttentionNeeded, limit = 50, offset = 0 } = queryResult.data;

    // Fetch sessions from Cosmos DB
    const result = await listSessions({
      status,
      humanAttentionNeeded: humanAttentionNeeded === 'true' ? true : humanAttentionNeeded === 'false' ? false : undefined,
      limit,
      offset,
    });

    // Sessions are already sorted by Cosmos DB query
    return apiResponse({
      sessions: result.sessions,
      total: result.total,
      limit,
      offset,
    });
  } catch (error) {
    console.error('[Sessions] List error:', error);
    return apiResponse(
      { error: 'Failed to list sessions', details: error instanceof Error ? error.message : 'Unknown error' },
      500
    );
  }
}

/**
 * POST /api/sessions
 * Create a new session
 */
export async function POST(request: NextRequest) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  try {
    // Parse and validate request body
    const result = await parseBody(request, CreateSessionRequestSchema);
    if (result.error) {
      return result.error;
    }

    const { externalSessionId, channel, metadata } = result.data;

    // Generate session ID
    const sessionId = `session-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;

    // Create session in Cosmos DB
    const session = await createSession({
      id: sessionId,
      externalSessionId,
      status: 'active',
      humanAttentionNeeded: false,
      channel,
      messageCount: 0,
      customerMessageCount: 0,
      aiAgentMessageCount: 0,
      operatorMessageCount: 0,
      metadata,
    });

    return apiResponse(
      {
        success: true,
        session,
      },
      201
    );
  } catch (error) {
    console.error('[Sessions] Create error:', error);
    return apiResponse(
      { error: 'Failed to create session', details: error instanceof Error ? error.message : 'Unknown error' },
      500
    );
  }
}
