import { afterEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';
import { getDeployment, ModuleDisabledError, parseDeployment, requireModule } from './deployment';
import { moduleUnavailable } from './api-utils';
import { caseRequestSchema, CaseService } from './case-service';
import { officialUrl, retrieveOrganizationKnowledge } from './organization-knowledge';
import { Dataverse } from './dataverse';
import { listSessions } from './cosmos-client';
import { agentAssistChat } from './openai-client';
import { GET as health } from '@/app/api/health/route';
import { POST as ingest } from '@/app/api/ingest/message/route';
import { POST as knowledge } from '@/app/api/knowledge/search/route';
import { GET as crmReadiness, POST as serviceAction } from '@/app/api/cases/route';

const original = structuredClone(getDeployment());
afterEach(() => {
  Object.assign(getDeployment(), structuredClone(original));
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function disableModules() {
  for (const feature of Object.keys(getDeployment().features) as (keyof typeof original.features)[]) {
    getDeployment().features[feature] = false;
  }
}

describe('packaged deployment configuration', () => {
  it('ships neutral defaults with no content, approved domains or enabled modules', async () => {
    const { default: raw } = await vi.importActual<{ default: typeof import('@/config/deployment.json') }>('@/config/deployment.json');
    const defaults = parseDeployment(raw);
    expect(defaults.organization).toMatchObject({ name: 'Contact center', defaultLanguage: 'en', timeZone: 'UTC' });
    expect(Object.values(defaults.features)).not.toContain(true);
    expect(defaults.features.nativeVoice).toBe(false);
    expect(defaults.business).toEqual({ caseCategories: ['general'], locationRequired: false, city: '' });
    expect(defaults.knowledge.allowedDomains).toEqual([]);
    expect(defaults.emailPolicy).toEqual({ allowedRecipients: [], allowedDomains: [] });
  });

  it('accepts injected organization, categories and domains without inventing defaults', () => {
    const config = structuredClone(original);
    config.organization = {
      name: 'Configured organization', assistantName: 'Service assistant',
      defaultLanguage: 'ar', timeZone: 'Europe/London',
      greeting: { en: 'Configured greeting', ar: 'مرحباً' },
    };
    config.business = { caseCategories: ['review'], locationRequired: false, city: 'Configured city' };
    config.knowledge.allowedDomains = ['approved.example.test'];
    expect(parseDeployment(config)).toEqual(config);
  });

  it('rejects invalid shapes, feature types, domains and time zones explicitly', () => {
    for (const mutate of [
      (config: Record<string, unknown>) => { config.schemaVersion = 2; },
      (config: Record<string, unknown>) => { config.customerRecords = []; },
      (config: Record<string, unknown>) => { config.features = { ...original.features, crm: 'false' }; },
      (config: Record<string, unknown>) => { config.business = { ...original.business, caseCategories: [] }; },
      (config: Record<string, unknown>) => { config.organization = { ...original.organization, timeZone: 'Invalid/Zone' }; },
      (config: Record<string, unknown>) => { config.knowledge = { allowedDomains: ['https://docs.example.test'] }; },
    ]) {
      const config: Record<string, unknown> = structuredClone(original);
      mutate(config);
      expect(() => parseDeployment(config)).toThrow('Invalid deployment.json');
    }
  });
});

describe('module isolation', () => {
  it('rejects disabled services before initialization and needs no optional credentials', async () => {
    disableModules();
    vi.stubEnv('API_KEY', '');
    vi.stubEnv('COSMOS_DB_ENDPOINT', '');
    vi.stubEnv('AZURE_OPENAI_ENDPOINT', '');
    vi.stubEnv('CRM_DATAVERSE_ORG_URL', '');
    vi.stubEnv('FOUNDRY_IQ_SEARCH_ENDPOINT', '');
    const fetcher = vi.fn().mockRejectedValue(new Error('No external requests permitted'));
    vi.stubGlobal('fetch', fetcher);
    expect(() => requireModule('crm')).toThrow(ModuleDisabledError);
    expect(moduleUnavailable('knowledge')?.status).toBe(503);
    await expect(new Dataverse().request('GET', 'WhoAmI')).rejects.toThrow(ModuleDisabledError);
    await expect(listSessions()).rejects.toThrow(ModuleDisabledError);
    await expect(agentAssistChat('General question', [])).rejects.toThrow(ModuleDisabledError);
    await expect(retrieveOrganizationKnowledge({ query: 'General question' })).rejects.toThrow(ModuleDisabledError);
    const healthResponse = await health(new NextRequest('http://localhost/api/health'));
    expect(healthResponse.status).toBe(200);
    expect((await healthResponse.json()).services).toMatchObject({
      cosmosDb: 'disabled', openAi: 'disabled', dynamicsCrm: 'disabled', knowledge: 'disabled',
    });
    for (const handler of [ingest, knowledge, serviceAction, crmReadiness]) {
      const response = await handler(new NextRequest('http://localhost/api/test', {
        method: handler === crmReadiness ? 'GET' : 'POST',
      }));
      expect(response.status).toBe(503);
      expect((await response.json()).error.code).toBe('MODULE_DISABLED');
    }
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('separates CRM and knowledge gates on their shared service endpoint', async () => {
    vi.stubEnv('API_KEY', 'unit-test-key');
    getDeployment().features.crm = false;
    getDeployment().features.knowledge = true;
    let response = await serviceAction(new NextRequest('http://localhost/api/cases', {
      method: 'POST', headers: { 'X-API-Key': 'unit-test-key' },
      body: JSON.stringify({ action: 'customer_context', actorId: `profile:${'a'.repeat(64)}` }),
    }));
    expect(response.status).toBe(503);
    expect((await response.json()).error.message).toContain('crm');
    getDeployment().features.crm = true;
    getDeployment().features.knowledge = false;
    response = await serviceAction(new NextRequest('http://localhost/api/cases', {
      method: 'POST', headers: { 'X-API-Key': 'unit-test-key' },
      body: JSON.stringify({ action: 'knowledge', query: 'General service question' }),
    }));
    expect(response.status).toBe(503);
    expect((await response.json()).error.message).toContain('knowledge');
  });

  it('uses only configured categories and location policy while preserving receipt shapes', async () => {
    getDeployment().business = { caseCategories: ['review'], locationRequired: false, city: 'Configured city' };
    const parsed = caseRequestSchema.parse({
      action: 'create_report', actorId: `profile:${'a'.repeat(64)}`, operationId: 'b'.repeat(64),
      category: 'review', description: 'Confirmed service issue', confirmed: true,
    });
    expect(parsed).toHaveProperty('location', { district: '', street: '', landmark: '' });
    expect(caseRequestSchema.safeParse({ ...parsed, category: 'unconfigured' }).success).toBe(false);
    const request = vi.fn();
    const receipt = await new CaseService({ request }).execute({
      action: 'resolve_location', location: { district: '', street: '', landmark: '' },
    });
    expect(receipt).toEqual({
      success: true, location: { city: 'Configured city', district: '', street: '', landmark: '' }, jurisdictionVerified: false,
    });
    expect(request).not.toHaveBeenCalled();
    getDeployment().business.locationRequired = true;
    expect(caseRequestSchema.safeParse(parsed).success).toBe(false);
  });

  it('denies retrieval when no approved domains exist, before authentication or fetch', async () => {
    getDeployment().knowledge.allowedDomains = [];
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    expect(officialUrl('https://docs.example.test/service')).toBe(false);
    await expect(retrieveOrganizationKnowledge({ query: 'Service question' })).rejects.toThrow('deployment-approved domains');
    expect(fetcher).not.toHaveBeenCalled();
  });
});
