import { NextRequest } from "next/server";
import { apiResponse, apiError, moduleUnavailable } from "@/lib/api-utils";
import { getInsights, getConversation, listMessages, upsertInsights, DBInsights } from "@/lib/cosmos-client";
import { generateInsights } from "@/lib/openai-client";
import type { ConversationInsights } from "@ccagent/shared";

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
    // Check if conversation exists
    const conversation = await getConversation(conversationId);
    if (!conversation) {
      return apiError("NOT_FOUND", "Conversation not found", 404);
    }

    // Try to get cached insights
    let insights: DBInsights | null = await getInsights(conversationId);

    // If no insights or stale, generate fresh ones
    const forceRefresh = request.nextUrl.searchParams.get("refresh") === "true";
    
    if (!insights || forceRefresh) {
      const { messages } = await listMessages(conversationId, { limit: 100 });
      
      if (messages.length > 0) {
        const generated = await generateInsights(conversationId, messages);
        insights = {
          conversationId,
          updatedAt: new Date().toISOString(),
          ...generated,
        };
        
        // Cache the insights
        await upsertInsights(insights);
      }
    }

    // If we still have no insights, return defaults
    if (!insights) {
      const defaultInsights: ConversationInsights = {
        conversationId,
        updatedAt: new Date().toISOString(),
        sentiment: {
          current: { label: "neutral", score: 0, confidence: 0.5 },
          history: [],
          frustrationScore: 0,
        },
        customerKPIs: {
          issueComplexity: "moderate",
          escalationRisk: "low",
          churnRisk: "low",
        },
        conversationKPIs: {
          durationSeconds: 0,
          estimatedHandleTimeSeconds: 300,
          customerMessageCount: conversation.messageCount || 0,
          agentMessageCount: 0,
          firstTimeResolutionLikelihood: 0.5,
          topicIntent: "General Inquiry",
        },
        complianceKPIs: {
          greetingDetected: false,
          identityVerified: false,
          disclaimerMentioned: false,
        },
        nextBestActions: [],
        summary: "Conversation analysis in progress...",
        extractedEntities: {
          productsMentioned: [],
          orderNumbers: [],
          ticketNumbers: [],
        },
      };
      return apiResponse(defaultInsights);
    }

    // Return the full insights object
    const response: ConversationInsights = {
      conversationId: insights.conversationId,
      updatedAt: insights.updatedAt || new Date().toISOString(),
      sentiment: insights.sentiment,
      customerKPIs: insights.customerKPIs,
      conversationKPIs: insights.conversationKPIs,
      complianceKPIs: insights.complianceKPIs,
      nextBestActions: insights.nextBestActions,
      summary: insights.summary,
      extractedEntities: insights.extractedEntities,
    };

    return apiResponse(response);
  } catch (err) {
    console.error("Failed to get insights:", err);
    return apiError("INTERNAL_ERROR", "Failed to retrieve insights", 500);
  }
}
