import { NextRequest } from "next/server";
import { z } from "zod";
import { apiError, apiResponse, parseBody, moduleUnavailable } from "@/lib/api-utils";
import { setConversationMembership, subscriberFromToken } from "@/lib/signalr-server";

const membershipSchema = z.object({
  conversationId: z.string().min(1).max(200),
  action: z.enum(["join", "leave"]),
}).strict();

export async function POST(request: NextRequest) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  if (!process.env.SIGNALR_CONNECTION_STRING) {
    return apiError("NOT_CONFIGURED", "Realtime messaging is not configured.", 503);
  }
  const token = request.headers.get("Authorization")?.match(/^Bearer ([A-Za-z0-9_.-]+)$/)?.[1];
  const subscriber = token ? subscriberFromToken(token) : null;
  if (!subscriber) {
    return apiError("UNAUTHORIZED", "A valid realtime subscriber token is required.", 401);
  }
  const parsed = await parseBody(request, membershipSchema);
  if (parsed.error) return parsed.error;
  try {
    await setConversationMembership(subscriber, parsed.data.conversationId, parsed.data.action === "join");
    return apiResponse({ success: true });
  } catch (error) {
    console.error("Realtime group subscription failed", error);
    return apiError("UPSTREAM_ERROR", "Could not update the realtime subscription.", 502);
  }
}
