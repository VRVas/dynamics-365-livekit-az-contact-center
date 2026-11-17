import { NextRequest } from "next/server";
import { MessageListQuerySchema } from "@ccagent/shared";
import { apiResponse, apiError, parseQuery, moduleUnavailable } from "@/lib/api-utils";
import { listMessages, getConversation } from "@/lib/cosmos-client";

interface RouteParams {
  params: Promise<{ conversationId: string }>;
}

export async function GET(request: NextRequest, { params }: RouteParams) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  const { conversationId } = await params;

  if (!conversationId) {
    return apiError("VALIDATION_ERROR", "Conversation ID is required", 400);
  }

  // Parse query parameters
  const { data: query, error } = parseQuery(
    request.nextUrl.searchParams,
    MessageListQuerySchema
  );
  if (error) return error;

  try {
    // Check if conversation exists
    const conversation = await getConversation(conversationId);
    if (!conversation) {
      return apiError("NOT_FOUND", "Conversation not found", 404);
    }

    // Fetch messages from Cosmos DB with pagination
    const limit = query.limit || 50;
    
    const { messages, total } = await listMessages(conversationId, {
      limit,
    });

    // Map DB fields to API response format (speaker/text)
    const response = {
      messages: messages.map((m) => ({
        messageId: m.id,
        conversationId: m.conversationId,
        timestamp: m.timestamp,
        speaker: m.sender, // Map sender to speaker for API
        channel: "chat" as const,
        text: m.content, // Map content to text for API
      })),
      hasMore: messages.length >= limit,
    };

    return apiResponse(response);
  } catch (err) {
    console.error("Failed to list messages:", err);
    return apiError("INTERNAL_ERROR", "Failed to retrieve messages", 500);
  }
}
