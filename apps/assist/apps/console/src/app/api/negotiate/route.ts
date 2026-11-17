import { NextRequest } from "next/server";
import { apiResponse, apiError, moduleUnavailable } from "@/lib/api-utils";
import { negotiate } from "@/lib/signalr-server";
import { randomUUID } from "crypto";

export async function POST(request: NextRequest) {
  const disabled = moduleUnavailable('agentAssist');
  if (disabled) return disabled;
  const connectionString = process.env.SIGNALR_CONNECTION_STRING;

  if (!connectionString) {
    return apiError("NOT_CONFIGURED", "Realtime messaging is not configured.", 503);
  }

  try {
    // An anonymous demo subscriber can control only its own connection identity.
    const connectionInfo = negotiate(randomUUID());

    return apiResponse(connectionInfo);
  } catch (err) {
    console.error("Failed to negotiate SignalR connection:", err);
    return apiError("INTERNAL_ERROR", "Failed to establish real-time connection", 500);
  }
}
