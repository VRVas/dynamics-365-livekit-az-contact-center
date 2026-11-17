import { NextRequest } from 'next/server';
import { z } from 'zod';
import { apiError, apiResponse, requireApiKey, validationError, moduleUnavailable } from '@/lib/api-utils';
import { caseRequestSchema, CaseService, ReportError } from '@/lib/case-service';
import { knowledgeRequestSchema, retrieveOrganizationKnowledge } from '@/lib/organization-knowledge';
import { DataverseError } from '@/lib/dataverse';
import { getDeployment } from '@/lib/deployment';

const cases = new CaseService();
const knowledge = knowledgeRequestSchema.extend({ action: z.literal('knowledge') });

export async function GET(request: NextRequest) {
  const disabled = moduleUnavailable('crm');
  if (disabled) return disabled;
  if (!process.env.API_KEY) return apiError('UNAVAILABLE', 'Service authentication is not configured.', 503);
  const authError = requireApiKey(request);
  if (authError) return authError;
  try {
    return apiResponse(await cases.readiness());
  } catch (error) {
    if (!(error instanceof DataverseError) && !(error instanceof z.ZodError)) throw error;
    console.error('CRM readiness failed', {
      type: error instanceof Error ? error.name : 'UnknownError',
      nativeCode: error instanceof DataverseError ? error.nativeCode : undefined,
    });
    return apiError('CRM_UNAVAILABLE', 'The Dynamics identity, customer/case schema or required native plug-in access is not ready.', 503);
  }
}

export async function POST(request: NextRequest) {
  const { features } = getDeployment();
  if (!features.crm && !features.knowledge) return apiError('MODULE_DISABLED', 'CRM and knowledge modules are disabled.', 503);
  if (!process.env.API_KEY) return apiError('UNAVAILABLE', 'Service authentication is not configured.', 503);
  const authError = requireApiKey(request);
  if (authError) return authError;
  try {
    const body: unknown = await request.json();
    const disabled = moduleUnavailable(
      body && typeof body === 'object' && 'action' in body && body.action === 'knowledge' ? 'knowledge' : 'crm',
    );
    if (disabled) return disabled;
    const requestBody = z.union([knowledge, caseRequestSchema]).parse(body);
    if (requestBody.action === 'knowledge') {
      const { action: _action, ...query } = requestBody;
      return apiResponse({ success: true, ...await retrieveOrganizationKnowledge(query) });
    }
    return apiResponse(await cases.execute(requestBody));
  } catch (error) {
    if (error instanceof z.ZodError) return validationError(error);
    if (error instanceof SyntaxError) return apiError('INVALID_JSON', 'Invalid JSON request.', 400);
    if (error instanceof ReportError) {
      console.warn('Case request rejected', { code: error.code });
      return apiError(error.code, error.message, error.status);
    }
    if (error instanceof DataverseError) {
      console.error('Dynamics case operation not confirmed', { code: error.code, status: error.status, nativeCode: error.nativeCode });
      return apiError(error.code, error.message, error.status === 404 ? 404 : 503);
    }
    console.error('Service operation failed', error instanceof Error ? error.message : 'Unknown service error');
    return apiError('SERVICE_UNAVAILABLE', 'The service could not confirm this operation. Do not claim completion; retrieve the request status before retrying a write.', 503);
  }
}
