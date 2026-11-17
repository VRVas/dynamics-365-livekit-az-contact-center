import { NextRequest } from "next/server";
import { AgentAssistChatRequestSchema } from "@ccagent/shared";
import { apiResponse, apiError, parseBody, moduleUnavailable } from "@/lib/api-utils";
import { agentAssistChat, type AICitation } from "@/lib/openai-client";
import { listMessages, getConversation } from "@/lib/cosmos-client";
import type { DBMessage } from "@/lib/cosmos-client";

export async function POST(request: NextRequest) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  // Parse and validate request body
  const { data, error } = await parseBody(request, AgentAssistChatRequestSchema);
  if (error) return error;

  try {
    // Get conversation context if conversationId provided
    let conversationContext: DBMessage[] = [];

    if (data.conversationId) {
      const conversation = await getConversation(data.conversationId);
      if (conversation) {
        const { messages } = await listMessages(data.conversationId, { limit: 20 });
        conversationContext = messages;
      }
    }

    const result = await agentAssistChat(
      data.question,
      conversationContext,
    );

    // Transform citations to API response format
    const response = {
      answer: result.message,
      suggestedResponses: result.suggestedResponses,
      citations: result.sources.map((source: AICitation) => ({
        id: source.id,
        title: source.title,
        content: source.snippet,
        source: "kb" as const, // Knowledge base source
        category: source.category,
        url: source.url,
        lastUpdated: new Date().toISOString(),
        relevanceScore: 0.9,
      })),
      confidence: result.sources.length > 0 ? 0.9 : 0.7,
    };

    return apiResponse(response);
  } catch (err) {
    console.error("Failed to process agent assist chat:", err);
    return apiError("INTERNAL_ERROR", "Failed to generate response", 500);
  }
}
