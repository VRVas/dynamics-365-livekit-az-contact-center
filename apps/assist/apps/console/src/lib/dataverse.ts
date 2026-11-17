import { ManagedIdentityCredential } from '@azure/identity';
import type { AccessToken } from '@azure/core-auth';
import { requireModule } from './deployment';

export class DataverseError extends Error {
  constructor(public readonly status: number, public readonly code: string, message: string, public readonly nativeCode?: string) {
    super(message);
  }
}

export interface DataverseTransport {
  request(method: 'GET' | 'PATCH', path: string, body?: unknown, headers?: Record<string, string>, timeoutMs?: number): Promise<unknown>;
}

export class Dataverse implements DataverseTransport {
  private credential?: ManagedIdentityCredential;
  private token?: AccessToken;
  private pendingToken?: Promise<AccessToken>;

  private async authorization(origin: string): Promise<string> {
    if (!process.env.CRM_CLIENT_ID) throw new DataverseError(503, 'CRM_UNAVAILABLE', 'The isolated CRM identity is not configured.');
    this.credential ??= new ManagedIdentityCredential(process.env.CRM_CLIENT_ID);
    if (!this.token || this.token.expiresOnTimestamp < Date.now() + 120000) {
      this.pendingToken ??= this.credential.getToken(`${origin}/.default`);
      try {
        this.token = await this.pendingToken;
      } finally {
        this.pendingToken = undefined;
      }
    }
    return `Bearer ${this.token.token}`;
  }

  async request(method: 'GET' | 'PATCH', path: string, body?: unknown, headers: Record<string, string> = {}, timeoutMs = method === 'PATCH' ? 10000 : 4000): Promise<unknown> {
    requireModule('crm');
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 10000) {
      throw new RangeError('The Dataverse request deadline must be between 1 and 10000 milliseconds.');
    }
    const origin = process.env.CRM_DATAVERSE_ORG_URL || '';
    if (!/^https:\/\/[a-z0-9-]+\.crm\d*\.dynamics\.com$/.test(origin)) {
      throw new DataverseError(503, 'CRM_UNAVAILABLE', 'The Dynamics CRM origin is not configured.');
    }
    const authorization = await this.authorization(origin);
    let response: Response;
    try {
      response = await fetch(`${origin}/api/data/v9.2/${path}`, {
        method, redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(timeoutMs),
        headers: {
          Authorization: authorization, Accept: 'application/json', 'OData-Version': '4.0',
          Prefer: 'odata.include-annotations="OData.Community.Display.V1.FormattedValue,Microsoft.Dynamics.CRM.lookuplogicalname"',
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...headers,
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch (error) {
      if (error instanceof TypeError || error instanceof DOMException) {
        throw new DataverseError(504, 'CRM_OUTCOME_UNKNOWN', 'Dynamics did not confirm the outcome. Retry only the same logical operation.');
      }
      throw error;
    }
    if (!response.ok) {
      let nativeCode: string | undefined;
      const errorText = await response.text();
      if (Buffer.byteLength(errorText) <= 16 * 1024) {
        try {
          const failure: unknown = JSON.parse(errorText);
          if (failure && typeof failure === 'object' && 'error' in failure) {
            const detail = failure.error;
            if (detail && typeof detail === 'object' && 'code' in detail &&
              typeof detail.code === 'string' && /^0x[0-9a-f]{8}$/i.test(detail.code)) {
              nativeCode = detail.code;
            }
          }
        } catch (error) {
          if (!(error instanceof SyntaxError)) throw error;
        }
      }
      throw new DataverseError(
        response.status, response.status === 429 ? 'CRM_THROTTLED' : 'CRM_REQUEST_FAILED',
        `Dynamics returned HTTP ${response.status}${nativeCode ? ` (${nativeCode})` : ''}; no replacement write was submitted.`,
        nativeCode,
      );
    }
    if (response.status === 204) return {};
    let text: string;
    try {
      text = await response.text();
    } catch (error) {
      if (error instanceof TypeError || error instanceof DOMException) {
        throw new DataverseError(504, 'CRM_OUTCOME_UNKNOWN', 'Dynamics did not finish its response. Reconcile the same logical operation before another write.');
      }
      throw error;
    }
    if (Buffer.byteLength(text) > 1024 * 1024) throw new DataverseError(502, 'CRM_RESPONSE_INVALID', 'The CRM result exceeds its size limit.');
    try {
      return text ? JSON.parse(text) : {};
    } catch (error) {
      if (error instanceof SyntaxError) throw new DataverseError(502, 'CRM_RESPONSE_INVALID', 'Dynamics returned invalid JSON.');
      throw error;
    }
  }
}
