import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DataverseError, type DataverseTransport } from './dataverse';
import { hash, operationGuid, caseRequestSchema, CaseService } from './case-service';
import { getDeployment } from './deployment';

const CUSTOMER = '11111111-1111-4111-8111-111111111111';
const CASE = '22222222-2222-4222-8222-222222222222';
const CONVERSATION = '33333333-3333-4333-8333-333333333333';
const WORKSTREAM = '44444444-4444-4444-8444-444444444444';
const OTHER = '55555555-5555-4555-8555-555555555555';
const actorId = `profile:${'a'.repeat(64)}`;
const customer = {
  contactid: CUSTOMER, fullname: 'Test Resident', firstname: 'Test',
  emailaddress1: 'resident@example.com', mobilephone: '+12025550101',
  description: 'Synthetic test customer', parentcustomerid_account: null,
  moacc_preferredlanguage: null,
};
const create = {
  action: 'create_report' as const, actorId, operationId: 'b'.repeat(64),
  category: 'general' as const, description: 'A service needs review.',
  location: { district: 'Test district', street: 'Test street', landmark: '' }, confirmed: true as const,
};
const key = hash(`${actorId}:${create.operationId}`);
const payloadHash = hash(JSON.stringify({ category: create.category, description: create.description, location: create.location }));
const record = {
  incidentid: CASE, ticketnumber: 'CAS-00001-TEST', title: 'Service issue',
  description: create.description, _customerid_value: CUSTOMER, statecode: 0, statuscode: 1,
  prioritycode: 2, createdon: '2026-01-01T00:00:00Z', modifiedon: '2026-01-02T00:00:00Z',
  moacc_requestkey: key, moacc_payloadhash: payloadHash, moacc_category: 'general',
  moacc_district: 'Test district', moacc_street: 'Test street', moacc_landmark: '',
  'statuscode@OData.Community.Display.V1.FormattedValue': 'In Progress',
};
const conversation = {
  _msdyn_liveworkstreamid_value: WORKSTREAM, _msdyn_customer_value: null,
  _msdyn_issueid_value: null, _regardingobjectid_value: null, statuscode: 2, '@odata.etag': 'W/"1"',
};
const linkedConversation = {
  ...conversation, _msdyn_customer_value: CUSTOMER, _msdyn_issueid_value: CASE,
  _regardingobjectid_value: CASE, '_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname': 'incident',
  '@odata.etag': 'W/"2"',
};

let request: ReturnType<typeof vi.fn<DataverseTransport['request']>>;
let service: CaseService;
beforeEach(() => {
  getDeployment().features.nativeVoice = true;
  request = vi.fn<DataverseTransport['request']>();
  service = new CaseService({ request });
});
afterEach(() => {
  getDeployment().features.nativeVoice = true;
  vi.unstubAllEnvs();
  vi.useRealTimers();
});
const foundCustomer = () => request.mockResolvedValueOnce({ value: [customer] });
const notes = () => request.mockResolvedValueOnce({ value: [{ notetext: 'Confirmed update', createdon: record.modifiedon }] });

describe('Dataverse case contract', () => {
  it('does not require native assemblies or voice tables for CRM-only readiness', async () => {
    getDeployment().features.nativeVoice = false;
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', 'unselected-native-configuration');
    request.mockResolvedValueOnce({ UserId: CUSTOMER });
    request.mockResolvedValueOnce({ value: [] });
    request.mockResolvedValueOnce({ value: [] });
    expect(await service.readiness()).toMatchObject({ crmReady: true, source: 'dynamics' });
    expect(request).toHaveBeenCalledTimes(3);
    expect(request.mock.calls.every(call => !/pluginassemblies|msdyn_/i.test(call[1]))).toBe(true);
  });

  it('checks the actual app identity and required CRM fields without a Cosmos request', async () => {
    request.mockResolvedValueOnce({ UserId: CUSTOMER });
    request.mockResolvedValueOnce({ value: [] });
    request.mockResolvedValueOnce({ value: [] });
    expect(await service.readiness()).toMatchObject({ crmReady: true, source: 'dynamics', cosmosCasePathEnabled: false });
    expect(request.mock.calls.every(call => call[0] === 'GET')).toBe(true);
    expect(request.mock.calls[1][1]).toContain('moacc_actorid');
    expect(request.mock.calls[2][1]).toContain('moacc_requestkey');
  });

  it('checks native plug-in readability when conversation linking is configured', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    request.mockResolvedValueOnce({ UserId: CUSTOMER });
    request.mockResolvedValueOnce({ value: [] });
    request.mockResolvedValueOnce({ value: [] });
    request.mockResolvedValueOnce({ value: [{
      pluginassemblyid: OTHER, name: 'Microsoft.Dynamics.OmnichannelPrime.Plugins',
    }] });
    expect(await service.readiness()).toMatchObject({ crmReady: true });
    const probe = new URL(`https://example.test/${request.mock.calls[3][1]}`);
    expect(probe.pathname).toBe('/pluginassemblies');
    expect(probe.searchParams.get('$select')).toBe('pluginassemblyid,name');
    expect(probe.searchParams.get('$filter')).toBe("name eq 'Microsoft.Dynamics.OmnichannelPrime.Plugins'");
    expect(request.mock.calls.every(call => call[0] === 'GET')).toBe(true);
  });

  it('refuses readiness when native plug-in access is missing', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    request.mockResolvedValueOnce({ UserId: CUSTOMER });
    request.mockResolvedValueOnce({ value: [] });
    request.mockResolvedValueOnce({ value: [] });
    request.mockRejectedValueOnce(new DataverseError(403, 'CRM_REQUEST_FAILED', 'Missing assembly read', '0x80040220'));
    await expect(service.readiness()).rejects.toMatchObject({ status: 403, nativeCode: '0x80040220' });
  });

  it('accepts genuine Dataverse GUID syntax without requiring RFC UUID version or variant bits', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', '44444444-4444-4444-7444-444444444444');
    request.mockResolvedValueOnce({ UserId: '11111111-1111-f111-8111-111111111111' });
    request.mockResolvedValueOnce({ value: [] });
    request.mockResolvedValueOnce({ value: [] });
    request.mockResolvedValueOnce({ value: [{
      pluginassemblyid: '55555555-5555-f111-8555-555555555555', name: 'Microsoft.Dynamics.OmnichannelPrime.Plugins',
    }] });
    expect(await service.readiness()).toMatchObject({ crmReady: true });
    expect(request).toHaveBeenCalledTimes(4);
    expect(request.mock.calls.every(call => call[0] === 'GET')).toBe(true);
  });

  it('refuses readiness when the native conversation plug-in is absent', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    request.mockResolvedValueOnce({ UserId: CUSTOMER });
    request.mockResolvedValueOnce({ value: [] });
    request.mockResolvedValueOnce({ value: [] });
    request.mockResolvedValueOnce({ value: [] });
    await expect(service.readiness()).rejects.toMatchObject({ code: 'CRM_NATIVE_PLUGIN_UNAVAILABLE' });
  });

  it('loads the customer and case context from Dynamics, including a synthetic-only guard', async () => {
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record], '@odata.count': 1 });
    const result = await service.execute({ action: 'customer_context', actorId });
    expect(result).toMatchObject({
      source: 'dynamics', dataScope: 'synthetic_profiles_only', identityVerified: false,
      customer: { id: CUSTOMER, name: customer.fullname, email: customer.emailaddress1 },
      cases: [{ reference: record.ticketnumber, status: 'In Progress' }], casesTruncated: false,
    });
    const filter = new URL(`https://example.test/${request.mock.calls[0][1]}`).searchParams.get('$filter');
    expect(filter).toContain(`moacc_actorid eq '${actorId}'`);
    expect(filter).toContain('moacc_demoenabled eq true');
    expect(new URL(`https://example.test/${request.mock.calls[0][1]}`).searchParams.get('$select')).toContain('moacc_preferredlanguage');
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('does not invent a profile or query private cases for an unknown caller', async () => {
    request.mockResolvedValueOnce({ value: [] });
    expect(await service.execute({ action: 'customer_context', actorId })).toMatchObject({ customer: null, cases: [] });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('returns the stored initial language without changing other customer data', async () => {
    request.mockResolvedValueOnce({ value: [{ ...customer, moacc_preferredlanguage: 'en' }] });
    request.mockResolvedValueOnce({ value: [] });
    expect(await service.execute({ action: 'customer_context', actorId }))
      .toMatchObject({ customer: { id: CUSTOMER, preferredLanguage: 'en' } });
  });

  it('refuses ambiguous customers and unlinked case creation', async () => {
    request.mockResolvedValueOnce({ value: [customer, { ...customer, contactid: OTHER }] });
    await expect(service.execute({ action: 'customer_context', actorId })).rejects.toMatchObject({ code: 'CUSTOMER_AMBIGUOUS' });
    request.mockResolvedValueOnce({ value: [] });
    await expect(service.execute(create)).rejects.toMatchObject({ code: 'CUSTOMER_NOT_FOUND' });
    expect(request.mock.calls.every(call => call[0] === 'GET')).toBe(true);
  });

  it('creates only once with a stable key and returns the real Dynamics case number', async () => {
    foundCustomer();
    request.mockResolvedValueOnce({});
    request.mockResolvedValueOnce({ ...record, incidentid: operationGuid(key) });
    notes();
    const result = await service.execute(create);
    expect(result).toMatchObject({ success: true, source: 'dynamics', reference: record.ticketnumber, externalSubmission: false });
    expect(request.mock.calls[1].slice(0, 4)).toEqual([
      'PATCH', `incidents(${operationGuid(key)})`,
      expect.objectContaining({ moacc_requestkey: key, 'customerid_contact@odata.bind': `/contacts(${CUSTOMER})` }),
      { 'If-None-Match': '*' },
    ]);
    foundCustomer();
    request.mockRejectedValueOnce(new DataverseError(412, 'PRECONDITION_FAILED', 'Already exists'));
    request.mockResolvedValueOnce({ ...record, incidentid: operationGuid(key) });
    notes();
    expect(await service.execute(create)).toEqual(result);
    expect(request.mock.calls.filter(call => call[0] === 'PATCH').every(call => call[3]?.['If-None-Match'] === '*')).toBe(true);
  });

  it('refuses an operation that already contains different data', async () => {
    foundCustomer();
    request.mockRejectedValueOnce(new DataverseError(412, 'PRECONDITION_FAILED', 'Already exists'));
    request.mockResolvedValueOnce({ ...record, moacc_payloadhash: 'c'.repeat(64) });
    await expect(service.execute(create)).rejects.toMatchObject({ code: 'IDEMPOTENCY_CONFLICT' });
  });

  it('reconciles an unknown create outcome by reading the same record without another write', async () => {
    foundCustomer();
    request.mockRejectedValueOnce(new DataverseError(504, 'CRM_OUTCOME_UNKNOWN', 'No response'));
    request.mockResolvedValueOnce({ ...record, incidentid: operationGuid(key) });
    notes();
    expect(await service.execute(create)).toMatchObject({ success: true, reference: record.ticketnumber });
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(1);
    expect(request.mock.calls[2][1]).toContain(`incidents(${operationGuid(key)})`);
  });

  it('waits for a delayed committed create and preserves every supplied location field', async () => {
    vi.useFakeTimers();
    foundCustomer();
    request.mockRejectedValueOnce(new DataverseError(504, 'CRM_OUTCOME_UNKNOWN', 'No response'));
    request.mockRejectedValueOnce(new DataverseError(404, 'CRM_REQUEST_FAILED', 'Not committed yet'));
    request.mockResolvedValueOnce({ ...record, incidentid: operationGuid(key) });
    notes();
    const pending = service.execute(create);
    await vi.runAllTimersAsync();
    expect(await pending).toMatchObject({ success: true, location: {
      district: create.location.district, street: create.location.street, landmark: create.location.landmark,
    } });
    const writes = request.mock.calls.filter(call => call[0] === 'PATCH');
    expect(writes).toHaveLength(1);
    expect(writes[0][2]).toMatchObject({
      moacc_category: create.category, moacc_district: create.location.district,
      moacc_street: create.location.street, moacc_landmark: create.location.landmark,
    });
  });

  it('keeps an absent delayed write explicitly unconfirmed, with bounded reads and no replacement write', async () => {
    vi.useFakeTimers();
    foundCustomer();
    request.mockRejectedValueOnce(new DataverseError(504, 'CRM_OUTCOME_UNKNOWN', 'No response'));
    request.mockRejectedValue(new DataverseError(404, 'CRM_REQUEST_FAILED', 'Still pending'));
    const pending = expect(service.execute(create)).rejects.toMatchObject({ code: 'CRM_WRITE_UNCONFIRMED' });
    await vi.runAllTimersAsync();
    await pending;
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(1);
    expect(request.mock.calls.filter(call => call[0] === 'GET')).toHaveLength(6);
    expect(request.mock.calls.at(-1)?.[4]).toBe(1000);
  });

  it('does not reconcile or hide an explicitly denied create', async () => {
    foundCustomer();
    request.mockRejectedValueOnce(new DataverseError(403, 'CRM_REQUEST_FAILED', 'Denied'));
    await expect(service.execute(create)).rejects.toMatchObject({ status: 403 });
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('budgets a slow native write and subsequent reads within the shared operation deadline', async () => {
    vi.useFakeTimers();
    const started = Date.now();
    request.mockImplementationOnce(async () => {
      vi.setSystemTime(started + 3000);
      return { value: [customer] };
    });
    request.mockImplementationOnce(async () => {
      vi.setSystemTime(started + 13000);
      return {};
    });
    request.mockResolvedValueOnce({ ...record, incidentid: operationGuid(key) });
    notes();
    expect(await service.execute(create)).toMatchObject({ success: true });
    expect(request.mock.calls.map(call => call[4])).toEqual([4000, 10000, 3000, 3000]);
  });

  it('does not let readback delays extend an exhausted operation deadline', async () => {
    vi.useFakeTimers();
    const started = Date.now();
    foundCustomer();
    request.mockImplementationOnce(async () => {
      vi.setSystemTime(started + 15500);
      throw new DataverseError(504, 'CRM_OUTCOME_UNKNOWN', 'Write unconfirmed');
    });
    request.mockRejectedValue(new DataverseError(404, 'CRM_REQUEST_FAILED', 'Not yet visible'));
    const result = expect(service.execute(create)).rejects.toMatchObject({ code: 'CRM_WRITE_UNCONFIRMED' });
    await vi.runAllTimersAsync();
    await result;
    expect(Date.now() - started).toBeLessThanOrEqual(16000);
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(1);
    expect(request.mock.calls.slice(2).every(call => call[4]! <= 500)).toBe(true);
  });

  it('filters a status or reconciliation lookup by the current customer', async () => {
    vi.useFakeTimers();
    foundCustomer();
    request.mockResolvedValue({ value: [] });
    const pending = expect(service.execute({ action: 'get_report', actorId, reference: `OP-${key.toUpperCase()}` }))
      .rejects.toMatchObject({ code: 'CRM_WRITE_UNCONFIRMED' });
    await vi.runAllTimersAsync();
    await pending;
    const filter = new URL(`https://example.test/${request.mock.calls[1][1]}`).searchParams.get('$filter');
    expect(filter).toBe(`moacc_requestkey eq '${key}' and _customerid_value eq ${CUSTOMER}`);
  });

  it('refuses a malformed cross-customer result even when its query was scoped', async () => {
    foundCustomer();
    request.mockResolvedValueOnce({ value: [{ ...record, _customerid_value: OTHER }] });
    await expect(service.execute({ action: 'get_report', actorId, reference: record.ticketnumber }))
      .rejects.toMatchObject({ code: 'CASE_NOT_FOUND' });
    foundCustomer();
    request.mockResolvedValueOnce({ value: [{ ...record, _customerid_value: OTHER }] });
    await expect(service.execute({ action: 'list_cases', actorId })).rejects.toMatchObject({ code: 'CRM_CUSTOMER_MISMATCH' });
  });

  it('adds a confirmed note, never a resolution/status update', async () => {
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    request.mockResolvedValueOnce({});
    request.mockResolvedValueOnce({ notetext: 'New details', _objectid_value: CASE });
    notes();
    await service.execute({ action: 'add_followup', actorId, operationId: 'c'.repeat(64), reference: record.ticketnumber, note: 'New details', confirmed: true });
    const writes = request.mock.calls.filter(call => call[0] === 'PATCH');
    expect(writes).toHaveLength(1);
    expect(writes[0][1]).toMatch(/^annotations\(/);
    expect(writes[0][2]).toEqual({
      subject: 'Customer update', notetext: 'New details', 'objectid_incident@odata.bind': `/incidents(${CASE})`,
    });
    expect(request.mock.calls.at(-1)?.[1]).toContain('subject+eq+%27Customer+update%27');
  });

  it('reconciles a committed follow-up after response loss without appending another note', async () => {
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    request.mockRejectedValueOnce(new DataverseError(504, 'CRM_OUTCOME_UNKNOWN', 'No response'));
    request.mockResolvedValueOnce({ notetext: 'New details', _objectid_value: CASE });
    notes();
    const result = await service.execute({
      action: 'add_followup', actorId, operationId: 'c'.repeat(64),
      reference: record.ticketnumber, note: 'New details', confirmed: true,
    });
    expect(result).toMatchObject({ success: true, reference: record.ticketnumber });
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(1);
  });

  it('links a matching native caller/workstream and maintains both native case association surfaces', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce(conversation);
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    request.mockResolvedValueOnce({});
    request.mockResolvedValueOnce(linkedConversation);
    const result = await service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION });
    expect(result).toMatchObject({ conversationLink: { status: 'linked' } });
    expect(request.mock.calls.find(call => call[0] === 'PATCH')?.slice(0, 4)).toEqual([
      'PATCH', `msdyn_ocliveworkitems(${CONVERSATION})`,
      {
        'msdyn_customer_msdyn_ocliveworkitem_contact@odata.bind': `/contacts(${CUSTOMER})`,
        'msdyn_IssueId_msdyn_ocliveworkitem@odata.bind': `/incidents(${CASE})`,
        'regardingobjectid_incident_msdyn_ocliveworkitem@odata.bind': `/incidents(${CASE})`,
      }, { 'If-Match': 'W/"1"' },
    ]);
    vi.unstubAllEnvs();
  });

  it('completes Case Details binding when IssueId is correct but Regarding still points to the customer', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce({
      ...linkedConversation, _regardingobjectid_value: CUSTOMER,
      '_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname': 'contact',
    });
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    request.mockResolvedValueOnce({});
    request.mockResolvedValueOnce(linkedConversation);
    expect(await service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION }))
      .toMatchObject({ conversationLink: { status: 'linked', alreadyLinked: false } });
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(1);
  });

  it('preserves all native case association guards for Dataverse sequential and non-RFC GUIDs', async () => {
    const ids = {
      customer: '11111111-1111-f111-7111-111111111111',
      incident: '22222222-2222-f111-7222-222222222222',
      conversation: '33333333-3333-f111-7333-333333333333',
      workstream: '44444444-4444-f111-7444-444444444444',
      account: '55555555-5555-f111-7555-555555555555',
    };
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', ids.workstream);
    request.mockResolvedValueOnce({ value: [{
      ...customer, contactid: ids.customer,
      parentcustomerid_account: { accountid: ids.account, name: 'Technical account', description: null },
    }] });
    request.mockResolvedValueOnce({ value: [{ ...record, incidentid: ids.incident, _customerid_value: ids.customer }] });
    notes();
    request.mockResolvedValueOnce({ ...conversation, _msdyn_liveworkstreamid_value: ids.workstream });
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    request.mockResolvedValueOnce({});
    request.mockResolvedValueOnce({
      ...linkedConversation, _msdyn_liveworkstreamid_value: ids.workstream,
      _msdyn_customer_value: ids.customer, _msdyn_issueid_value: ids.incident, _regardingobjectid_value: ids.incident,
    });
    expect(await service.execute({
      action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: ids.conversation,
    })).toMatchObject({ success: true, conversationLink: { status: 'linked' } });
    expect(request.mock.calls.find(call => call[0] === 'PATCH')?.slice(0, 4)).toEqual([
      'PATCH', `msdyn_ocliveworkitems(${ids.conversation})`,
      {
        'msdyn_customer_msdyn_ocliveworkitem_contact@odata.bind': `/contacts(${ids.customer})`,
        'msdyn_IssueId_msdyn_ocliveworkitem@odata.bind': `/incidents(${ids.incident})`,
        'regardingobjectid_incident_msdyn_ocliveworkitem@odata.bind': `/incidents(${ids.incident})`,
      }, { 'If-Match': 'W/"1"' },
    ]);
  });

  it('rejects a foreign well-formed Dataverse GUID workstream without writing', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', '44444444-4444-f111-7444-444444444444');
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce({
      ...conversation, _msdyn_liveworkstreamid_value: '55555555-5555-f111-7555-555555555555',
    });
    expect(await service.execute({
      action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION,
    })).toMatchObject({ conversationLink: { status: 'failed', error: 'CONVERSATION_MISMATCH' } });
    expect(request.mock.calls.every(call => call[0] === 'GET')).toBe(true);
  });

  it('preserves an unrelated Regarding record rather than replacing it with a case', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce({
      ...conversation, _regardingobjectid_value: OTHER,
      '_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname': 'account',
    });
    expect(await service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION }))
      .toMatchObject({ conversationLink: { status: 'failed', error: 'CONVERSATION_MISMATCH' } });
    expect(request.mock.calls.every(call => call[0] === 'GET')).toBe(true);
  });

  it('preserves a concurrent newer case selection after a version conflict', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce(conversation);
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    request.mockRejectedValueOnce(new DataverseError(412, 'CRM_REQUEST_FAILED', 'Version changed'));
    request.mockResolvedValueOnce({ ...linkedConversation, _msdyn_issueid_value: OTHER, _regardingobjectid_value: OTHER });
    expect(await service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION }))
      .toMatchObject({ conversationLink: { status: 'failed', error: 'CONVERSATION_LINK_CHANGED' } });
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(1);
  });

  it('revalidates and retries a changed native ETag without dropping concurrency protection', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce(conversation);
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    request.mockRejectedValueOnce(new DataverseError(412, 'CRM_REQUEST_FAILED', 'Version changed'));
    request.mockResolvedValueOnce({ ...conversation, '@odata.etag': 'W/"3"' });
    request.mockResolvedValueOnce({});
    request.mockResolvedValueOnce(linkedConversation);
    const result = await service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION });
    expect(result).toMatchObject({ conversationLink: { status: 'linked' } });
    expect(request.mock.calls.filter(call => call[0] === 'PATCH').map(call => call[3])).toEqual([
      { 'If-Match': 'W/"1"' }, { 'If-Match': 'W/"3"' },
    ]);
    vi.unstubAllEnvs();
  });

  it('does not report linking from an accepted PATCH without matching readback', async () => {
    vi.useFakeTimers();
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce(conversation);
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    request.mockResolvedValueOnce({});
    request.mockResolvedValue(conversation);
    const pending = service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION });
    await vi.runAllTimersAsync();
    const result = await pending;
    expect(result).toMatchObject({ success: true, conversationLink: { status: 'failed', stage: 'readback', error: 'CRM_WRITE_UNCONFIRMED', outcome: 'unknown' } });
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(1);
    vi.unstubAllEnvs();
  });

  it('reconciles a timed-out native link with readback rather than issuing another association write', async () => {
    vi.useFakeTimers();
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce(conversation);
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    request.mockRejectedValueOnce(new DataverseError(504, 'CRM_OUTCOME_UNKNOWN', 'No response'));
    request.mockResolvedValueOnce(conversation);
    request.mockResolvedValueOnce(linkedConversation);
    const pending = service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION });
    await vi.runAllTimersAsync();
    expect(await pending).toMatchObject({ success: true, conversationLink: { status: 'linked' } });
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(1);
  });

  it('does not rewrite a completed conversation to make a historical test pass', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce({ ...conversation, statuscode: 4 });
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    const result = await service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION });
    expect(result).toMatchObject({ conversationLink: { status: 'failed', error: 'CONVERSATION_NOT_ACTIVE' } });
    expect(request.mock.calls.every(call => call[0] === 'GET')).toBe(true);
    vi.unstubAllEnvs();
  });

  it('stops retrying if the conversation customer changes concurrently', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce(conversation);
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    request.mockRejectedValueOnce(new DataverseError(412, 'CRM_REQUEST_FAILED', 'Version changed'));
    request.mockResolvedValueOnce({ ...conversation, _msdyn_customer_value: OTHER });
    const result = await service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION });
    expect(result).toMatchObject({ conversationLink: { status: 'failed', error: 'CONVERSATION_MISMATCH' } });
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(1);
    vi.unstubAllEnvs();
  });

  it('bounds native version-conflict retries to three attempts', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockResolvedValueOnce(conversation);
    request.mockResolvedValueOnce({ value: [{ msdyn_fromphone: customer.mobilephone }] });
    request.mockRejectedValueOnce(new DataverseError(412, 'CRM_REQUEST_FAILED', 'Version changed'));
    for (let attempt = 0; attempt < 2; attempt++) {
      request.mockResolvedValueOnce(conversation);
      request.mockRejectedValueOnce(new DataverseError(412, 'CRM_REQUEST_FAILED', 'Version changed'));
    }
    const result = await service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION });
    expect(result).toMatchObject({ conversationLink: { status: 'failed', stage: 'patch', httpStatus: 412 } });
    expect(request.mock.calls.filter(call => call[0] === 'PATCH')).toHaveLength(3);
    vi.unstubAllEnvs();
  });

  it('reports a link failure separately without pretending the saved case was lost', async () => {
    vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', WORKSTREAM);
    foundCustomer();
    request.mockResolvedValueOnce({ value: [record] });
    notes();
    request.mockRejectedValueOnce(new DataverseError(403, 'CRM_ACCESS_DENIED', 'Denied'));
    const result = await service.execute({ action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION });
    expect(result).toMatchObject({ success: true, reference: record.ticketnumber, conversationLink: { status: 'failed', error: 'CRM_ACCESS_DENIED' } });
    expect(request.mock.calls.every(call => call[0] === 'GET')).toBe(true);
  });

  it('requires both nativeVoice and a workstream allowlist before accessing native tables', async () => {
    for (const [enabled, allowlist, code] of [
      [false, WORKSTREAM, 'MODULE_DISABLED'],
      [true, '', 'NATIVE_WORKSTREAM_UNCONFIGURED'],
      [true, 'invalid', 'CRM_NATIVE_CONFIG_INVALID'],
      [true, '44444444-4444-f111-7444-444444444444,44444444-4444-F111-7444-444444444444', 'CRM_NATIVE_CONFIG_INVALID'],
    ] as const) {
      request.mockReset();
      getDeployment().features.nativeVoice = enabled;
      vi.stubEnv('CRM_NATIVE_WORKSTREAM_IDS', allowlist);
      foundCustomer();
      request.mockResolvedValueOnce({ value: [record] });
      notes();
      const result = await service.execute({
        action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: CONVERSATION,
      });
      expect(result).toMatchObject({
        success: true, reference: record.ticketnumber,
        conversationLink: { status: 'failed', error: code, stage: 'configuration', outcome: 'rejected' },
      });
      expect(request).toHaveBeenCalledTimes(3);
      expect(request.mock.calls.every(call => call[0] === 'GET' && !call[1].startsWith('msdyn_'))).toBe(true);
    }
  });

  it('requires confirmation and rejects actor/query/status injection', () => {
    for (const invalid of [
      { ...create, confirmed: false }, { ...create, actorId: 'other' },
      { ...create, status: 'resolved' }, { ...create, location: { district: 'Test', street: '', landmark: '' } },
      { action: 'get_report', actorId, reference: "CAS' or true" },
      { action: 'get_report', actorId, reference: record.ticketnumber, nativeConversationId: "33333333-3333-f111-7333-333333333333' or true" },
    ]) expect(caseRequestSchema.safeParse(invalid).success).toBe(false);
  });
});
