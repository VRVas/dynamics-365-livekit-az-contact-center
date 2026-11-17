import { NextRequest } from "next/server";
import { IngestMessageRequestSchema } from "@ccagent/shared";
import { apiResponse, apiError, parseBody, requireApiKey, moduleUnavailable } from "@/lib/api-utils";
import {
  createMessage,
  getSession,
  ensureSessionExists,
} from "@/lib/cosmos-client";
import { notifyNewMessage, notifyConversationUpdate, notifyInsightsUpdated } from "@/lib/signalr-server";
import { generateInsights, } from "@/lib/openai-client";
import { upsertInsights, listMessages } from "@/lib/cosmos-client";

export async function POST(request: NextRequest) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  // Check API key for adapter authentication
  const authError = requireApiKey(request);
  if (authError) return authError;

  // Parse and validate request body
  const { data, error } = await parseBody(request, IngestMessageRequestSchema);
  if (error) return error;

  try {
    // Use session_id (preferred) or conversation_id (deprecated)
    const sessionId = data.session_id || data.conversation_id!;
    
    console.log(`[Ingest] Processing message ${data.message_id} for session ${sessionId}`);

    // Check if this is a new session
    let existingSession;
    let isNewSession;
    try {
      console.log("[Ingest] Checking existing session...");
      existingSession = await getSession(sessionId);
      isNewSession = !existingSession;
      console.log(`[Ingest] Is new session: ${isNewSession}`);
    } catch (getErr) {
      console.error("[Ingest] getSession failed:", getErr);
      throw getErr;
    }

    // Ensure session exists (creates if new)
    let session;
    try {
      console.log("[Ingest] Ensuring session exists...");
      session = await ensureSessionExists(sessionId, data.channel);
      console.log("[Ingest] Session ensured:", session?.id);
    } catch (ensureErr) {
      console.error("[Ingest] ensureSessionExists failed:", ensureErr);
      throw ensureErr;
    }

    // Store message in Cosmos DB (this also updates session message counts)
    let message;
    try {
      console.log("[Ingest] Creating message...");
      message = await createMessage({
        id: data.message_id,
        sessionId: sessionId,
        sender: data.speaker,
        content: data.text,
        contentType: "text",
        metadata: data.metadata,
      });
      console.log("[Ingest] Message created:", message?.id);
    } catch (createErr) {
      console.error("[Ingest] createMessage failed:", createErr);
      throw createErr;
    }

    // Broadcast via SignalR
    try {
      console.log("[Ingest] Broadcasting via SignalR...");
      await notifyNewMessage(sessionId, {
        id: message.id,
        sender: message.sender,
        content: message.content,
        timestamp: message.timestamp,
      });

      if (isNewSession) {
        await notifyConversationUpdate(
          sessionId,
          "active",
          "conversation_started"
        );
      }
      console.log("[Ingest] SignalR broadcast complete");
    } catch (signalrError) {
      // Log but don't fail the request if SignalR broadcast fails
      console.warn("SignalR broadcast failed:", signalrError);
    }

    // Generate insights asynchronously (fire and forget for now)
    generateInsightsAsync(sessionId).catch((err) =>
      console.warn("Insights generation failed:", err)
    );

    const response = {
      success: true,
      messageId: data.message_id,
      sessionId: sessionId,
      conversationId: sessionId, // Deprecated: same as sessionId
      isNewSession,
      isNewConversation: isNewSession, // Deprecated: same as isNewSession
    };

    console.log("[Ingest] Success:", response);
    return apiResponse(response, 201);
  } catch (err) {
    console.error("Failed to ingest message:", err);
    console.error("Error details:", JSON.stringify(err, Object.getOwnPropertyNames(err)));
    return apiError("INTERNAL_ERROR", "Failed to process message", 500);
  }
}

// Helper to generate insights in background
async function generateInsightsAsync(sessionId: string) {
  const { messages } = await listMessages(sessionId, { limit: 50 });
  if (messages.length === 0) return;

  const insights = await generateInsights(sessionId, messages);
  await upsertInsights({
    conversationId: sessionId, // Keep conversationId field for backward compatibility
    updatedAt: new Date().toISOString(),
    ...insights,
  });

  // Notify clients of updated insights via proper SignalR event
  try {
    await notifyInsightsUpdated(sessionId, {
      sentiment: insights.sentiment?.current?.label || 'neutral',
      topics: [insights.conversationKPIs?.topicIntent || ''].filter(Boolean),
      suggestedActions: insights.nextBestActions?.map(a => a.label) || [],
    });
    console.log(`[Ingest] Insights updated notification sent for ${sessionId}`);
  } catch (err) {
    console.warn('[Ingest] Failed to notify insights update:', err);
  }
}
