import { NextRequest } from "next/server";
import { apiResponse, apiError, moduleUnavailable } from "@/lib/api-utils";
import { getConversation, listMessages } from "@/lib/cosmos-client";

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

  try {
    // Fetch conversation from Cosmos DB
    const conversation = await getConversation(conversationId);

    if (!conversation) {
      return apiError("NOT_FOUND", "Conversation not found", 404);
    }

    // Fetch messages for this conversation
    const { messages } = await listMessages(conversationId, { limit: 100 });

    // Return conversation with messages
    return apiResponse({
      conversationId: conversation.id,
      createdAt: conversation.createdAt,
      updatedAt: conversation.updatedAt,
      status: conversation.status,
      channel: conversation.channel || "chat",
      customerId: conversation.customerId,
      messageCount: conversation.messageCount || messages.length,
      messages: messages.map((m) => ({
        messageId: m.id,
        sender: m.sender,
        content: m.content,
        contentType: m.contentType || "text",
        timestamp: m.timestamp,
        metadata: m.metadata,
      })),
    });
  } catch (err) {
    console.error("Failed to get conversation:", err);
    return apiError("INTERNAL_ERROR", "Failed to retrieve conversation", 500);
  }
}
