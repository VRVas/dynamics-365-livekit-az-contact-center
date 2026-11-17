import { DefaultAzureCredential } from '@azure/identity';
import { z } from 'zod';
import { getDeployment, requireModule } from './deployment';

export const KNOWLEDGE_API_VERSION = '2026-04-01';
export const knowledgeRequestSchema = z.object({
  query: z.string().trim().min(3).max(1200),
  locale: z.enum(['en', 'ar']).default(() => getDeployment().organization.defaultLanguage),
  sensitiveTerms: z.array(z.string().max(200)).max(30).default([]),
}).strict();

export interface KnowledgeEvidence {
  found: boolean;
  grounding: string;
  sources: { reference_id: string; title: string; source: string }[];
  elapsedMs: number;
  partial?: boolean;
  omittedPassages?: number;
  warnings?: string[];
}
const sourceSchema = z.object({
  id: z.string(),
  type: z.enum(['web', 'searchIndex']),
  title: z.string().nullish(),
  url: z.string().nullish(),
  sourceData: z.record(z.string(), z.unknown()).nullish(),
}).passthrough();
const responseSchema = z.object({
  response: z.array(z.object({ content: z.array(z.object({ type: z.literal('text'), text: z.string() })) })),
  references: z.array(sourceSchema),
  activity: z.array(z.object({ error: z.unknown().optional(), warning: z.string().nullish() })).optional(),
});
let credential: DefaultAzureCredential | undefined;

export function officialUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password &&
      getDeployment().knowledge.allowedDomains.some((domain) => url.hostname === domain || url.hostname.endsWith(`.${domain}`));
  } catch {
    return false;
  }
}

export function publicQuery(query: string, sensitiveTerms: string[]): string {
  let safe = query.normalize('NFKC')
    .replace(/https?:\/\/\S+|[^\s@]+@[^\s@]+\.[^\s@]+|(?:CAS|OP)-[a-z\d-]+/giu, ' ')
    .replace(/[+\d\u0660-\u0669\u06f0-\u06f9][\d\u0660-\u0669\u06f0-\u06f9\s().-]{5,}/gu, ' ');
  for (const term of sensitiveTerms.filter((value) => value.trim().length > 1)) {
    safe = safe.replaceAll(new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu'), ' ');
  }
  safe = safe.replace(/\s+/g, ' ').trim();
  if (safe.length < 3) throw new Error('Ask a general service question without personal or request identifiers.');
  return safe;
}

export function retrievalRequest(query: string, mode: 'web' | 'indexed', source: string, webSource = 'approved-web', locale = 'en') {
  const domains = getDeployment().knowledge.allowedDomains;
  const scopedQuery = mode === 'web' && domains.length ? `${query} (${domains.map(domain => `site:${domain}`).join(' OR ')})` : query;
  return {
    // GA explicit intents avoid the additional preview query-planning model call.
    intents: [{ type: 'semantic', search: scopedQuery }],
    maxRuntimeInSeconds: 10,
    maxOutputSizeInTokens: 6000,
    includeActivity: true,
    knowledgeSourceParams: [{
      kind: 'searchIndex', knowledgeSourceName: source,
      filterAddOn: "approvalStatus eq 'approved'",
      includeReferences: true, includeReferenceSourceData: true,
    }, ...(mode === 'web' ? [{
      kind: 'web', knowledgeSourceName: webSource, count: 3,
      language: locale, includeReferences: true, includeReferenceSourceData: false,
    }] : [])],
  };
}

export function normalizeEvidence(payload: unknown, elapsedMs: number): KnowledgeEvidence {
  const parsed = responseSchema.parse(payload);
  if (parsed.activity?.some((activity) => activity.error)) {
    throw new Error('An official knowledge source failed. A complete retrieval could not be confirmed.');
  }
  const warnings = parsed.activity?.flatMap((activity) => activity.warning ? [activity.warning] : []) || [];
  const grounding = parsed.response.flatMap((message) => message.content.map((part) => part.text)).join('\n').trim();
  const sources = parsed.references.map((reference) => {
    const source = reference.url ?? reference.sourceData?.sourceUrl;
    const title = reference.title ?? reference.sourceData?.title;
    if (typeof source !== 'string' || !officialUrl(source)) throw new Error('Knowledge retrieval returned a nonofficial or missing source URL.');
    if (reference.type === 'searchIndex' && reference.sourceData?.approvalStatus !== 'approved') throw new Error('Knowledge retrieval returned unapproved indexed content.');
    return { reference_id: reference.id, title: typeof title === 'string' ? title : new URL(source).hostname, source };
  });
  if ((sources.length > 0) !== (grounding !== '' && grounding !== '[]')) {
    throw new Error('Knowledge retrieval returned incomplete citation evidence.');
  }
  const result: KnowledgeEvidence = { found: sources.length > 0, grounding: sources.length ? grounding : '', sources, elapsedMs, warnings };
  if (grounding.startsWith('[') && grounding !== '[]') {
    const passages = z.array(z.object({
      ref_id: z.union([z.string(), z.number()]),
      content: z.string(),
    })).parse(JSON.parse(grounding));
    const byId = new Map(sources.map((source) => [source.reference_id, source]));
    const selected: { id: string; content: string }[] = [];
    const used = new Set<string>();
    const notice = 'Some matching passages were omitted to fit this response. This is not an exhaustive requirements checklist; refine the query when more detail is needed.\n\n';
    for (const passage of passages) {
      const id = String(passage.ref_id);
      if (!byId.has(id)) throw new Error('A retrieved passage has no matching official citation.');
      const candidate = [...selected, { id, content: passage.content }];
      const ids = new Set([...used, id]);
      const candidateSources = sources.filter((source) => ids.has(source.reference_id));
      const text = candidate.map((item) => `[Source ${item.id}]\n${item.content}`).join('\n\n');
      const budgeted = { ...result, grounding: notice + text, sources: candidateSources, partial: true, omittedPassages: passages.length };
      if (Buffer.byteLength(JSON.stringify(budgeted), 'utf8') <= 12 * 1024) {
        selected.push({ id, content: passage.content });
        used.add(id);
      }
    }
    if (!selected.length) throw new Error('No complete evidence passage fits the voice response limit. Please ask a narrower question.');
    result.partial = selected.length < passages.length;
    result.omittedPassages = passages.length - selected.length;
    result.grounding = (result.partial ? notice : '') + selected.map((item) => `[Source ${item.id}]\n${item.content}`).join('\n\n');
    result.sources = sources.filter((source) => used.has(source.reference_id));
  }
  if (Buffer.byteLength(JSON.stringify(result), 'utf8') > 12 * 1024) {
    throw new Error('Knowledge evidence exceeds the voice response limit. Please ask a more focused question.');
  }
  return result;
}

export async function retrieveOrganizationKnowledge(
  input: z.input<typeof knowledgeRequestSchema>,
  options: { indexedOnly?: boolean } = {},
): Promise<KnowledgeEvidence> {
  requireModule('knowledge');
  if (!getDeployment().knowledge.allowedDomains.length) throw new Error('Knowledge retrieval requires deployment-approved domains.');
  const { query, locale, sensitiveTerms } = knowledgeRequestSchema.parse(input);
  const endpoint = process.env.FOUNDRY_IQ_SEARCH_ENDPOINT;
  const source = process.env.FOUNDRY_IQ_KNOWLEDGE_SOURCE;
  const webSource = process.env.FOUNDRY_IQ_WEB_SOURCE || '';
  const mode = options.indexedOnly ? 'indexed' : (process.env.FOUNDRY_IQ_MODE || 'web');
  if (mode !== 'web' && mode !== 'indexed') throw new Error('FOUNDRY_IQ_MODE must be web or indexed.');
  const base = mode === 'indexed' ? process.env.FOUNDRY_IQ_INDEXED_KNOWLEDGE_BASE : process.env.FOUNDRY_IQ_KNOWLEDGE_BASE;
  if (!endpoint || !base || !source || (mode === 'web' && !webSource)) throw new Error('Knowledge retrieval is not configured.');
  const url = new URL(endpoint);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash ||
    !/^[a-z\d-]+\.search\.windows\.net$/i.test(url.hostname) || !/^[a-z\d_-]+$/i.test(base) ||
    !/^[a-z\d_-]+$/i.test(source) || (mode === 'web' && !/^[a-z\d_-]+$/i.test(webSource))) {
    throw new Error('Invalid knowledge service configuration.');
  }
  const safe = publicQuery(query, sensitiveTerms);
  const sourceLanguage = /[\u0600-\u06ff]/u.test(safe) ? 'ar' : locale;
  credential ??= new DefaultAzureCredential({ managedIdentityClientId: process.env.AZURE_CLIENT_ID });
  const token = await credential.getToken('https://search.azure.com/.default');
  if (!token) throw new Error('Knowledge service authentication failed.');
  const start = performance.now();
  const response = await fetch(`${endpoint.replace(/\/$/, '')}/knowledgebases('${base}')/retrieve?api-version=${KNOWLEDGE_API_VERSION}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token.token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(retrievalRequest(safe, mode, source, webSource, sourceLanguage)),
    signal: AbortSignal.timeout(14000), redirect: 'error', cache: 'no-store',
  });
  if (!response.ok) throw new Error(`Knowledge service returned HTTP ${response.status}.`);
  const payload: unknown = await response.json();
  if (typeof payload !== 'object' || payload === null || !('activity' in payload) || !Array.isArray(payload.activity)) {
    throw new Error('Knowledge retrieval omitted the requested source-status activity.');
  }
  const result = normalizeEvidence(payload, Math.round(performance.now() - start));
  console.info('organization_knowledge', { mode, elapsedMs: result.elapsedMs, sourceCount: result.sources.length });
  return result;
}
