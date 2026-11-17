import { NextRequest } from "next/server";
import { apiResponse } from "@/lib/api-utils";
import { getDeployment } from "@/lib/deployment";

export async function GET(request: NextRequest) {
  const { features } = getDeployment();
  const health = {
    status: "healthy",
    check: "process-and-configuration",
    timestamp: new Date().toISOString(),
    version: process.env.npm_package_version || "0.1.0",
    features,
    services: {
      cosmosDb: !features.agentAssist ? "disabled" : process.env.COSMOS_DB_ENDPOINT ? "configured" : "not-configured",
      openAi: !features.agentAssist ? "disabled" : process.env.AZURE_OPENAI_ENDPOINT ? "configured" : "not-configured",
      signalR: !features.agentAssist ? "disabled" : process.env.SIGNALR_CONNECTION_STRING ? "configured" : "not-configured",
      blobStorage: process.env.AZURE_STORAGE_CONNECTION_STRING ? "configured" : "not-used",
      dynamicsCrm: !features.crm ? "disabled" : process.env.CRM_DATAVERSE_ORG_URL && process.env.CRM_CLIENT_ID ? "configured" : "not-configured",
      knowledge: !features.knowledge ? "disabled" : process.env.FOUNDRY_IQ_SEARCH_ENDPOINT ? "configured" : "not-configured",
      cosmosCasePath: "disabled",
    },
  };

  return apiResponse(health);
}
