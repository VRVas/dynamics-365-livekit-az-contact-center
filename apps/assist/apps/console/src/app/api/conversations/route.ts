import { NextRequest } from "next/server";
import { ConversationListQuerySchema } from "@ccagent/shared";
import { apiResponse, parseQuery, moduleUnavailable } from "@/lib/api-utils";
import { listConversations } from "@/lib/cosmos-client";
import type { SessionStatus } from "@ccagent/shared";

export async function GET(request: NextRequest) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  // Parse query parameters
  const { data: query, error } = parseQuery(
    request.nextUrl.searchParams,
    ConversationListQuerySchema
  );
  if (error) return error;

  try {
    // Fetch from Cosmos DB with filters
    // Map legacy status values to new SessionStatus
    const statusMap: Record<string, SessionStatus> = {
      'active': 'active',
      'closed': 'completed',
      'on-hold': 'active',
    };
    const dbStatus = query.status ? statusMap[query.status] : undefined;
    
    const { conversations, total } = await listConversations({
      limit: query.limit || 20,
      status: dbStatus,
    });

    // Map to response format
    const response = {
      conversations: conversations.map((conv) => ({
        conversationId: conv.id,
        createdAt: conv.createdAt,
        updatedAt: conv.updatedAt,
        status: conv.status,
        messageCount: conv.messageCount || 0,
        channel: conv.channel || "chat",
      })),
      continuationToken: query.continuationToken 
        ? String(conversations.length) 
        : undefined,
    };

    return apiResponse(response);
  } catch (err) {
    console.error("Failed to list conversations:", err);
    return apiResponse({ conversations: [] });
  }
}
