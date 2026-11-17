import { NextRequest, NextResponse } from 'next/server';
import { z } from 'zod';
import { apiResponse, parseBody, moduleUnavailable } from '@/lib/api-utils';
import { getSession, updateSession, deleteSessionCascade } from '@/lib/cosmos-client';

// Update session request schema
const UpdateSessionRequestSchema = z.object({
  status: z.enum(['active', 'completed', 'abandoned']).optional(),
  humanAttentionNeeded: z.boolean().optional(),
  escalationReason: z.string().optional(),
}).refine(data => Object.keys(data).length > 0, {
  message: 'At least one field must be provided for update',
});

interface RouteContext {
  params: Promise<{ sessionId: string }>;
}

/**
 * GET /api/sessions/{sessionId}
 * Get session details by ID
 */
export async function GET(
  request: NextRequest,
  context: RouteContext
) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  try {
    const { sessionId } = await context.params;

    if (!sessionId) {
      return apiResponse({ error: 'Session ID is required' }, 400);
    }

    const session = await getSession(sessionId);

    if (!session) {
      return apiResponse({ error: 'Session not found' }, 404);
    }

    return apiResponse({ session });
  } catch (error) {
    console.error('[Sessions] Get error:', error);
    return apiResponse(
      { error: 'Failed to get session', details: error instanceof Error ? error.message : 'Unknown error' },
      500
    );
  }
}

/**
 * PATCH /api/sessions/{sessionId}
 * Update session status, escalation, or humanAttentionNeeded flag
 */
export async function PATCH(
  request: NextRequest,
  context: RouteContext
) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  try {
    const { sessionId } = await context.params;

    if (!sessionId) {
      return apiResponse({ error: 'Session ID is required' }, 400);
    }

    // Parse and validate request body
    const result = await parseBody(request, UpdateSessionRequestSchema);
    if (result.error) {
      return result.error;
    }

    const updates = result.data;
    const now = new Date().toISOString();

    // Build update object
    const updateData: Record<string, unknown> = {
      ...updates,
      updatedAt: now,
    };

    // If escalating (setting humanAttentionNeeded to true), record the timestamp
    if (updates.humanAttentionNeeded === true) {
      updateData.humanAttentionRequestedAt = now;
    }

    // If completing/abandoning, record the timestamp
    if (updates.status === 'completed' || updates.status === 'abandoned') {
      updateData.completedAt = now;
    }

    const updatedSession = await updateSession(sessionId, updateData);

    if (!updatedSession) {
      return apiResponse({ error: 'Session not found' }, 404);
    }

    return apiResponse({
      success: true,
      session: updatedSession,
    });
  } catch (error) {
    console.error('[Sessions] Update error:', error);
    return apiResponse(
      { error: 'Failed to update session', details: error instanceof Error ? error.message : 'Unknown error' },
      500
    );
  }
}

/**
 * DELETE /api/sessions/{sessionId}
 * Permanently delete a session and its messages + insights (cascade).
 */
export async function DELETE(
  request: NextRequest,
  context: RouteContext
) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  try {
    const { sessionId } = await context.params;

    if (!sessionId) {
      return apiResponse({ error: 'Session ID is required' }, 400);
    }

    const { deleted, messagesDeleted } = await deleteSessionCascade(sessionId);

    if (!deleted) {
      return apiResponse({ error: 'Session not found' }, 404);
    }

    return apiResponse({ success: true, sessionId, messagesDeleted });
  } catch (error) {
    console.error('[Sessions] Delete error:', error);
    return apiResponse(
      { error: 'Failed to delete session', details: error instanceof Error ? error.message : 'Unknown error' },
      500
    );
  }
}
