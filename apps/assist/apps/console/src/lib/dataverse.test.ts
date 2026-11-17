import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Dataverse } from './dataverse';

const credential = vi.hoisted(() => ({
  getToken: vi.fn(),
}));
vi.mock('@azure/identity', () => ({
  ManagedIdentityCredential: class {
    getToken = credential.getToken;
  },
}));

const fetcher = vi.fn<typeof fetch>();
beforeEach(() => {
  vi.stubEnv('CRM_DATAVERSE_ORG_URL', 'https://example.crm17.dynamics.com');
  vi.stubEnv('CRM_CLIENT_ID', '11111111-1111-4111-8111-111111111111');
  credential.getToken.mockReset().mockResolvedValue({ token: 'synthetic-token', expiresOnTimestamp: Date.now() + 3600000 });
  fetcher.mockReset().mockImplementation(async () => new Response(JSON.stringify({ value: [] }), { status: 200 }));
  vi.stubGlobal('fetch', fetcher);
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe('isolated Dataverse transport', () => {
  it('caches the CRM identity token and uses the selected organization scope', async () => {
    const crm = new Dataverse();
    await Promise.all([crm.request('GET', 'contacts?$top=1'), crm.request('GET', 'incidents?$top=1')]);
    expect(credential.getToken).toHaveBeenCalledTimes(1);
    expect(credential.getToken).toHaveBeenCalledWith('https://example.crm17.dynamics.com/.default');
    expect(fetcher.mock.calls[0][0]).toBe('https://example.crm17.dynamics.com/api/data/v9.2/contacts?$top=1');
    expect(fetcher.mock.calls[0][1]).toMatchObject({
      cache: 'no-store', redirect: 'error', headers: { Authorization: 'Bearer synthetic-token' },
    });
  });

  it('does not fall back to the shared identity when CRM is unconfigured', async () => {
    vi.stubEnv('CRM_CLIENT_ID', '');
    await expect(new Dataverse().request('GET', 'contacts')).rejects.toMatchObject({ code: 'CRM_UNAVAILABLE' });
    credential.getToken.mockClear();
    vi.stubEnv('CRM_DATAVERSE_ORG_URL', 'https://example.invalid/private');
    await expect(new Dataverse().request('GET', 'contacts')).rejects.toMatchObject({ code: 'CRM_UNAVAILABLE' });
    expect(credential.getToken).not.toHaveBeenCalled();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('preserves create-only conditional headers and accepts a confirmed no-content response', async () => {
    fetcher.mockResolvedValueOnce(new Response(null, { status: 204 }));
    expect(await new Dataverse().request('PATCH', 'incidents(test-id)', { title: 'Synthetic case' }, { 'If-None-Match': '*' })).toEqual({});
    expect(fetcher.mock.calls[0][1]).toMatchObject({
      method: 'PATCH', body: JSON.stringify({ title: 'Synthetic case' }), headers: { 'If-None-Match': '*' },
    });
  });

  it('surfaces HTTP failures and does not retry writes automatically', async () => {
    for (const status of [403, 412, 429, 503]) {
      fetcher.mockResolvedValueOnce(new Response('private error details', { status }));
      await expect(new Dataverse().request('PATCH', 'incidents(test-id)', {})).rejects.toMatchObject({ status });
    }
    expect(fetcher).toHaveBeenCalledTimes(4);
  });

  it('retains the native error code without exposing private error text', async () => {
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({
      error: { code: '0x80040224', message: 'Private native record and identity details' },
    }), { status: 400 }));
    const result = new Dataverse().request('PATCH', 'msdyn_ocliveworkitems(test-id)', {});
    await expect(result).rejects.toMatchObject({
      status: 400, code: 'CRM_REQUEST_FAILED', nativeCode: '0x80040224',
      message: 'Dynamics returned HTTP 400 (0x80040224); no replacement write was submitted.',
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('does not expose malformed or oversized native error codes', async () => {
    for (const body of [
      JSON.stringify({ error: { code: 'private record text' } }),
      JSON.stringify({ error: { code: '0x80040224', message: 'x'.repeat(16 * 1024) } }),
    ]) {
      fetcher.mockResolvedValueOnce(new Response(body, { status: 400 }));
      await expect(new Dataverse().request('PATCH', 'incidents(test-id)', {}))
        .rejects.toMatchObject({ status: 400, code: 'CRM_REQUEST_FAILED', nativeCode: undefined });
    }
  });

  it('marks transport failure as an unknown outcome instead of confirmed non-creation', async () => {
    fetcher.mockRejectedValueOnce(new TypeError('Synthetic connection loss'));
    await expect(new Dataverse().request('PATCH', 'incidents(test-id)', {}))
      .rejects.toMatchObject({ code: 'CRM_OUTCOME_UNKNOWN', status: 504 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('rejects malformed response JSON', async () => {
    fetcher.mockResolvedValueOnce(new Response('not-json', { status: 200 }));
    await expect(new Dataverse().request('GET', 'contacts')).rejects.toMatchObject({ code: 'CRM_RESPONSE_INVALID' });
  });

  it('treats a response-body timeout as unconfirmed rather than a new write failure', async () => {
    const response = new Response('{}', { status: 200 });
    vi.spyOn(response, 'text').mockRejectedValueOnce(new DOMException('Deadline reached', 'TimeoutError'));
    fetcher.mockResolvedValueOnce(response);
    await expect(new Dataverse().request('GET', 'incidents(test-id)'))
      .rejects.toMatchObject({ status: 504, code: 'CRM_OUTCOME_UNKNOWN' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('honors a remaining readback budget without extending the normal transport deadline', async () => {
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    await new Dataverse().request('GET', 'incidents(test-id)', undefined, undefined, 500);
    expect(timeout).toHaveBeenLastCalledWith(500);
    for (const invalid of [0, 10001, 0.5, Number.NaN]) {
      await expect(new Dataverse().request('GET', 'incidents(test-id)', undefined, undefined, invalid))
        .rejects.toThrow(RangeError);
    }
    expect(fetcher).toHaveBeenCalledTimes(1);
    timeout.mockRestore();
  });

  it('gives native writes a bounded confirmation window without slowing ordinary read deadlines', async () => {
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    const crm = new Dataverse();
    await crm.request('PATCH', 'incidents(test-id)', {}, { 'If-None-Match': '*' });
    expect(timeout).toHaveBeenLastCalledWith(10000);
    await crm.request('GET', 'incidents(test-id)');
    expect(timeout).toHaveBeenLastCalledWith(4000);
    timeout.mockRestore();
  });
});
