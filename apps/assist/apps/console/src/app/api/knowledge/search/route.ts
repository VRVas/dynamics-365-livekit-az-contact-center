import { NextRequest } from "next/server";
import { KnowledgeSearchRequestSchema } from "@ccagent/shared";
import { apiResponse, apiError, parseBody, moduleUnavailable } from "@/lib/api-utils";
import { retrieveOrganizationKnowledge } from "@/lib/organization-knowledge";

export async function POST(request: NextRequest) {
  const disabled = moduleUnavailable('knowledge');
  if (disabled) return disabled;
  // Parse and validate request body
  const { data, error } = await parseBody(request, KnowledgeSearchRequestSchema);
  if (error) return error;

  try {
    const evidence = await retrieveOrganizationKnowledge({ query: data.query }, { indexedOnly: true });
    return apiResponse({
      results: evidence.sources.slice(0, data.topK).map((source) => ({
        id: source.reference_id, title: source.title,
        content: data.includeContent ? source.title : '',
        source: source.source, url: source.source, category: 'Service knowledge',
      })),
      totalCount: evidence.sources.length,
      grounding: evidence.grounding,
    });
  } catch (error) {
    console.error('Knowledge search failed', error instanceof Error ? error.message : 'Unknown error');
    return apiError('KNOWLEDGE_UNAVAILABLE', 'Official knowledge could not be retrieved.', 503);
  }
}
