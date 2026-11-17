import { afterEach, describe, expect, it, vi } from 'vitest';
import { normalizeEvidence, officialUrl, publicQuery, retrievalRequest, retrieveOrganizationKnowledge } from './organization-knowledge';

vi.mock('@azure/identity', () => ({
  DefaultAzureCredential: class {
    async getToken() { return { token: 'unit-test-token' }; }
  },
}));

const evidence = () => ({
  response: [{ content: [{ type: 'text', text: 'Approved technical guidance [0].' }] }],
  references: [{ id: '0', type: 'web', title: 'Service guidance', url: 'https://docs.example.test/service' }],
  activity: [],
});

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe('official organization knowledge', () => {
  it('preserves real web references without requiring an index-only classification', () => {
    expect(normalizeEvidence(evidence(), 125)).toMatchObject({
      found: true, elapsedMs: 125,
      sources: [{ reference_id: '0', title: 'Service guidance', source: 'https://docs.example.test/service' }],
    });
  });
  it('accepts omitted or null source data for web references', () => {
    const payload = evidence();
    expect(normalizeEvidence({ ...payload, references: payload.references.map((reference) => ({ ...reference, sourceData: null })) }, 1).found).toBe(true);
  });
  it('uses direct intents without a redundant planning call and bounds web results', () => {
    const web = retrievalRequest('service requirements', 'web', 'approved-documents');
    expect(web).not.toHaveProperty('messages');
    expect(web).not.toHaveProperty('retrievalReasoningEffort');
    expect(web.maxOutputSizeInTokens).toBe(6000);
    expect(web).not.toHaveProperty('maxOutputDocuments');
    expect(web.knowledgeSourceParams[1]).toMatchObject({ kind: 'web', count: 3, includeReferences: true });
    const indexed = retrievalRequest('service requirements', 'indexed', 'approved-documents');
    expect(indexed).not.toHaveProperty('retrievalReasoningEffort');
    expect(indexed.knowledgeSourceParams).toHaveLength(1);
    if (!('intents' in indexed)) throw new Error('Indexed retrieval must use explicit intents.');
    expect(indexed.intents).toEqual([{ type: 'semantic', search: 'service requirements' }]);
    expect(web.intents[0].search).toContain('site:docs.example.test');
  });
  it('removes supplied private terms, email, phone, and case identifiers', () => {
    const result = publicQuery('PRIVATE_NAME contact@example.test +1 202 555 0101 CAS-ABC123 asks about services', ['PRIVATE_NAME']);
    expect(result).toBe('asks about services');
    expect(publicQuery('متابعة ٠٥٥١٢٣٤٥٦٧ وخدمات الرخص', [])).not.toMatch(/[٠-٩]/);
  });
  it('rejects deceptive source domains and unsafe URL schemes', () => {
    for (const url of ['https://docs.example.test.evil.example/service', 'http://docs.example.test', 'https://user@docs.example.test']) {
      expect(officialUrl(url)).toBe(false);
    }
    expect(officialUrl('https://sub.docs.example.test/service')).toBe(true);
  });
  it('rejects uncited, unapproved, unrelated, malformed and oversized evidence', () => {
    expect(() => normalizeEvidence({ ...evidence(), references: [] }, 1)).toThrow();
    expect(() => normalizeEvidence({ ...evidence(), references: [{ id: '0', type: 'searchIndex', sourceData: { sourceUrl: 'https://docs.example.test', approvalStatus: 'pending' } }] }, 1)).toThrow();
    expect(() => normalizeEvidence({ ...evidence(), references: [{ id: '0', type: 'web', url: 'https://example.com' }] }, 1)).toThrow();
    expect(() => normalizeEvidence({ response: [] }, 1)).toThrow();
    expect(() => normalizeEvidence({ ...evidence(), activity: [{ error: { code: 'Timeout' } }] }, 1)).toThrow(/source failed/);
    expect(() => normalizeEvidence({ ...evidence(), response: [{ content: [{ type: 'text', text: 'ن'.repeat(13000) }] }] }, 1)).toThrow(/limit/);
    expect(normalizeEvidence({ response: [], references: [] }, 1).found).toBe(false);
  });
  it('packs complete ranked passages, preserving their citations and explicitly marking omissions', () => {
    const result = normalizeEvidence({
      response: [{ content: [{ type: 'text', text: JSON.stringify([
        { ref_id: 0, content: 'A'.repeat(5000) },
        { ref_id: 1, content: 'B'.repeat(5000) },
        { ref_id: 2, content: 'C'.repeat(5000) },
      ]) }] }],
      references: [0, 1, 2].map((id) => ({ id: String(id), type: 'web', url: `https://docs.example.test/services/${id}` })),
    }, 1);
    expect(result.partial).toBe(true);
    expect(result.omittedPassages).toBe(1);
    expect(result.sources.map((source) => source.reference_id)).toEqual(['0', '1']);
    expect(result.grounding).toContain('A'.repeat(5000));
    expect(Buffer.byteLength(JSON.stringify(result))).toBeLessThanOrEqual(12 * 1024);
  });
  it('uses managed identity, strips private content and never sends the conversation', async () => {
    vi.stubEnv('FOUNDRY_IQ_SEARCH_ENDPOINT', 'https://service-test.search.windows.net');
    vi.stubEnv('FOUNDRY_IQ_KNOWLEDGE_BASE', 'service-knowledge');
    vi.stubEnv('FOUNDRY_IQ_KNOWLEDGE_SOURCE', 'approved-documents');
    vi.stubEnv('FOUNDRY_IQ_WEB_SOURCE', 'approved-web');
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(evidence())));
    vi.stubGlobal('fetch', fetcher);
    await retrieveOrganizationKnowledge({ query: 'PRIVATE_NAME asks about service requirements', sensitiveTerms: ['PRIVATE_NAME'], locale: 'ar' });
    const body = JSON.parse(fetcher.mock.calls[0][1].body);
    expect(body.intents[0].search).toBe('asks about service requirements (site:docs.example.test)');
    expect(body.knowledgeSourceParams[1].language).toBe('ar');
    expect(fetcher.mock.calls[0][1].headers.Authorization).toBe('Bearer unit-test-token');
    expect(fetcher.mock.calls[0][1].redirect).toBe('error');
  });
  it('does not turn service failures into successful answers', async () => {
    vi.stubEnv('FOUNDRY_IQ_SEARCH_ENDPOINT', 'https://service-test.search.windows.net');
    vi.stubEnv('FOUNDRY_IQ_KNOWLEDGE_BASE', 'service-knowledge');
    vi.stubEnv('FOUNDRY_IQ_KNOWLEDGE_SOURCE', 'approved-documents');
    vi.stubEnv('FOUNDRY_IQ_WEB_SOURCE', 'approved-web');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 503 })));
    await expect(retrieveOrganizationKnowledge({ query: 'service requirements' })).rejects.toThrow('503');
  });
});
