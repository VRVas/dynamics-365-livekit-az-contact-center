import { createHash } from 'node:crypto';
import { z } from 'zod';
import { Dataverse, DataverseError, type DataverseTransport } from './dataverse';
import { getDeployment, requireModule } from './deployment';

const text = (max: number) => z.string().trim().min(1).max(max);
export const locationSchema = z.object({
  district: z.string().trim().max(120).default(''),
  street: z.string().trim().max(160).default(''),
  landmark: z.string().trim().max(200).default(''),
}).strict().refine(value => !getDeployment().business.locationRequired || (value.district && (value.street || value.landmark)), {
  message: 'A district and street or nearby landmark are required to identify the location.',
});
const identity = {
  actorId: z.string().regex(/^(profile|session):[a-f0-9]{64}$/),
  nativeConversationId: z.guid().optional(),
};
const reference = z.string().trim().toUpperCase().regex(/^[A-Z0-9][A-Z0-9-]{2,79}$/);
const category = text(100).refine(value => getDeployment().business.caseCategories.includes(value), 'Choose a configured case category.');
export const caseRequestSchema = z.discriminatedUnion('action', [
  z.object({ action: z.literal('resolve_location'), location: z.preprocess(value => value ?? {}, locationSchema) }).strict(),
  z.object({ action: z.literal('customer_context'), ...identity }).strict(),
  z.object({ action: z.literal('list_cases'), ...identity }).strict(),
  z.object({
    action: z.literal('create_report'), ...identity, operationId: z.string().regex(/^[a-f0-9]{64}$/),
    category, description: text(2000), location: z.preprocess(value => value ?? {}, locationSchema), confirmed: z.literal(true),
  }).strict(),
  z.object({ action: z.literal('get_report'), ...identity, reference }).strict(),
  z.object({
    action: z.literal('add_followup'), ...identity, operationId: z.string().regex(/^[a-f0-9]{64}$/),
    reference, note: text(2000), confirmed: z.literal(true),
  }).strict(),
]);
type Request = z.infer<typeof caseRequestSchema>;
const contactRow = z.object({
  contactid: z.guid(), fullname: z.string(), firstname: z.string().nullable(),
  emailaddress1: z.string().nullable(), mobilephone: z.string(), description: z.string().nullable(),
  moacc_preferredlanguage: z.enum(['ar', 'en']).nullable(),
  parentcustomerid_account: z.object({ accountid: z.guid(), name: z.string(), description: z.string().nullable() }).nullable().optional(),
});
const caseRow = z.object({
  incidentid: z.guid(), ticketnumber: z.string(), title: z.string(), description: z.string().nullable(),
  _customerid_value: z.guid(), statecode: z.number().int(), statuscode: z.number().int(),
  prioritycode: z.number().int(), createdon: z.string(), modifiedon: z.string(),
  moacc_requestkey: z.string().nullable(), moacc_payloadhash: z.string().nullable(),
  moacc_category: z.string().nullable(), moacc_district: z.string().nullable(),
  moacc_street: z.string().nullable(), moacc_landmark: z.string().nullable(),
  'statuscode@OData.Community.Display.V1.FormattedValue': z.string().optional(),
});
type Contact = z.infer<typeof contactRow>;
type Case = z.infer<typeof caseRow>;
const caseFields = [
  'incidentid', 'ticketnumber', 'title', 'description', '_customerid_value', 'statecode', 'statuscode',
  'prioritycode', 'createdon', 'modifiedon', 'moacc_requestkey', 'moacc_payloadhash',
  'moacc_category', 'moacc_district', 'moacc_street', 'moacc_landmark',
].join(',');
export const hash = (value: string) => createHash('sha256').update(value).digest('hex');
export function operationGuid(key: string): string {
  const value = hash(key);
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-5${value.slice(13, 16)}-a${value.slice(17, 20)}-${value.slice(20, 32)}`;
}
const query = (table: string, parameters: Record<string, string>) => `${table}?${new URLSearchParams(parameters)}`;
const READBACK_WINDOW_MS = 6000;
const READBACK_DELAYS_MS = [0, 250, 750, 1500, 2500];
const OPERATION_WINDOW_MS = 16000;
const uncertainWrite = (error: unknown): error is DataverseError => error instanceof DataverseError &&
  (error.code === 'CRM_OUTCOME_UNKNOWN' || error.status === 408 || error.status >= 500);

function nativeWorkstreams(): string[] {
  const values = (process.env.CRM_NATIVE_WORKSTREAM_IDS || '').split(',').map(value => value.trim().toLowerCase()).filter(Boolean);
  if (new Set(values).size !== values.length || values.some(value => !z.guid().safeParse(value).success)) {
    throw new DataverseError(503, 'CRM_NATIVE_CONFIG_INVALID', 'CRM_NATIVE_WORKSTREAM_IDS must contain distinct workstream GUIDs.');
  }
  return values;
}

export class ReportError extends Error {
  constructor(public readonly code: string, message: string, public readonly status: number) { super(message); }
}

export class CaseService {
  constructor(private readonly crm: DataverseTransport = new Dataverse(), private readonly deadline = Infinity) {}

  async readiness() {
    requireModule('crm');
    z.object({ UserId: z.guid() }).parse(await this.crm.request('GET', 'WhoAmI'));
    for (const [table, fields] of [
      ['contacts', 'contactid,moacc_actorid,moacc_demoenabled,moacc_preferredlanguage'],
      ['incidents', 'incidentid,moacc_requestkey,moacc_payloadhash,moacc_category,moacc_district,moacc_street,moacc_landmark'],
    ]) {
      z.object({ value: z.array(z.object({})) }).parse(await this.crm.request('GET', query(table, {
        '$select': fields, '$top': '1',
      })));
    }
    if (getDeployment().features.nativeVoice && nativeWorkstreams().length) {
      const assembly = 'Microsoft.Dynamics.OmnichannelPrime.Plugins';
      const result = z.object({ value: z.array(z.object({
        pluginassemblyid: z.guid(), name: z.literal(assembly),
      })) }).parse(await this.crm.request('GET', query('pluginassemblies', {
        '$select': 'pluginassemblyid,name', '$filter': `name eq '${assembly}'`, '$top': '1',
      })));
      if (result.value.length !== 1) {
        throw new DataverseError(503, 'CRM_NATIVE_PLUGIN_UNAVAILABLE', 'The native conversation plug-in assembly is unavailable.');
      }
    }
    return { source: 'dynamics', crmReady: true, cosmosCasePathEnabled: false, dataScope: 'synthetic_profiles_only' };
  }

  private async customer(actor: string): Promise<Contact | null> {
    const result = z.object({ value: z.array(contactRow) }).parse(await this.crm.request('GET', query('contacts', {
      '$select': 'contactid,fullname,firstname,emailaddress1,mobilephone,description,moacc_preferredlanguage',
      '$expand': 'parentcustomerid_account($select=accountid,name,description)',
      '$filter': `moacc_actorid eq '${actor}' and moacc_demoenabled eq true and statecode eq 0`, '$top': '2',
    })));
    if (result.value.length > 1) throw new ReportError('CUSTOMER_AMBIGUOUS', 'More than one customer matches. Ask a representative to identify the correct record.', 409);
    return result.value[0] ?? null;
  }

  private receipt(row: Case) {
    return {
      success: true, source: 'dynamics', reference: row.ticketnumber, caseId: row.incidentid,
      title: row.title, status: row['statuscode@OData.Community.Display.V1.FormattedValue'] ?? String(row.statuscode),
      stateCode: row.statecode, statusCode: row.statuscode, priorityCode: row.prioritycode,
      category: row.moacc_category, description: row.description,
      location: { district: row.moacc_district, street: row.moacc_street, landmark: row.moacc_landmark },
      createdAt: row.createdon, updatedAt: row.modifiedon, externalSubmission: false,
      instruction: 'This is a Dynamics case. Do not claim external submission, dispatch, approval or a completion date.',
    };
  }

  private async cases(customer: Contact) {
    const result = z.object({ value: z.array(caseRow), '@odata.count': z.number().optional() }).parse(
      await this.crm.request('GET', query('incidents', {
        '$select': caseFields, '$filter': `_customerid_value eq ${customer.contactid}`,
        '$orderby': 'modifiedon desc', '$top': '5', '$count': 'true',
      })),
    );
    if (result.value.some(row => row._customerid_value !== customer.contactid)) {
      throw new ReportError('CRM_CUSTOMER_MISMATCH', 'Dynamics returned a case outside the bound customer context.', 502);
    }
    return {
      cases: result.value.map(row => this.receipt(row)),
      casesTruncated: (result['@odata.count'] ?? result.value.length) > result.value.length,
    };
  }

  private async read(reference: string, customer: Contact): Promise<Case> {
    const operationLookup = /^OP-[A-F0-9]{64}$/.test(reference);
    const filter = operationLookup
      ? `moacc_requestkey eq '${reference.slice(3).toLowerCase()}'`
      : `ticketnumber eq '${reference}'`;
    const read = async (timeoutMs = 4000) => {
      const result = z.object({ value: z.array(caseRow) }).parse(await this.crm.request('GET', query('incidents', {
        '$select': caseFields, '$filter': `${filter} and _customerid_value eq ${customer.contactid}`, '$top': '2',
      }), undefined, undefined, timeoutMs));
      if (operationLookup && !result.value.length) {
        throw new DataverseError(404, 'CRM_OPERATION_PENDING', 'The submitted creation is not visible yet.');
      }
      if (result.value.length !== 1 || result.value[0]._customerid_value !== customer.contactid) {
        throw new ReportError('CASE_NOT_FOUND', 'No unique case was found for this customer and reference.', 404);
      }
      return result.value[0];
    };
    return operationLookup ? this.readBack(read) : read();
  }

  private async readBack<T>(read: (timeoutMs: number) => Promise<T>, pending: (value: T) => boolean = () => false): Promise<T> {
    const deadline = Math.min(Date.now() + READBACK_WINDOW_MS, this.deadline);
    for (const wait of READBACK_DELAYS_MS) {
      const remaining = deadline - Date.now();
      if (remaining <= wait) break;
      if (wait) await new Promise<void>(resolve => setTimeout(resolve, wait));
      try {
        const value = await read(Math.max(1, Math.min(4000, deadline - Date.now())));
        if (!pending(value)) return value;
      } catch (error) {
        if (!(error instanceof DataverseError) || (error.status !== 404 && !uncertainWrite(error))) throw error;
      }
    }
    console.warn('Dynamics write readback remains unconfirmed after its bounded reconciliation window');
    throw new DataverseError(
      504, 'CRM_WRITE_UNCONFIRMED',
      'Dynamics may already have saved the operation, but its result is not confirmed yet. Do not say it was not recorded. Check the same operation reference; do not create a replacement.',
    );
  }

  private async createOnly<T>(path: string, body: unknown, read: (timeoutMs: number) => Promise<T>): Promise<T> {
    try {
      await this.crm.request('PATCH', path, body, { 'If-None-Match': '*' });
    } catch (error) {
      if (!(error instanceof DataverseError) || (error.status !== 412 && !uncertainWrite(error))) throw error;
      if (error.status !== 412) {
        console.warn('Reconciling an uncertain Dynamics create without repeating its write', { code: error.code, status: error.status });
      }
    }
    return this.readBack(read);
  }

  private async link(row: Case, customer: Contact, request: Request) {
    if (!('nativeConversationId' in request) || !request.nativeConversationId) return { status: 'not_requested' };
    const conversationId = request.nativeConversationId;
    const schema = z.object({
      '_msdyn_liveworkstreamid_value': z.guid(),
      '_msdyn_customer_value': z.guid().nullable(),
      '_msdyn_issueid_value': z.guid().nullable(),
      '_regardingobjectid_value': z.guid().nullable(),
      '_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname': z.string().optional(),
      statuscode: z.number().int(),
      '@odata.etag': z.string(),
    });
    const conversationPath = query(`msdyn_ocliveworkitems(${conversationId})`, {
      '$select': '_msdyn_liveworkstreamid_value,_msdyn_customer_value,_msdyn_issueid_value,_regardingobjectid_value,statuscode',
    });
    const regardingIs = (conversation: z.infer<typeof schema>, id: string, type: string) =>
      conversation._regardingobjectid_value === id &&
      conversation['_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname'] === type;
    const isLinked = (conversation: z.infer<typeof schema>) =>
      conversation._msdyn_customer_value === customer.contactid &&
      conversation._msdyn_issueid_value === row.incidentid && regardingIs(conversation, row.incidentid, 'incident');
    let originalAssociation: { caseId: string | null; regardingId: string | null; regardingType: string | undefined } | undefined;
    const compatibleAssociation = (conversation: z.infer<typeof schema>) =>
      (conversation._msdyn_issueid_value === originalAssociation?.caseId || conversation._msdyn_issueid_value === row.incidentid) &&
      ((conversation._regardingobjectid_value === originalAssociation?.regardingId &&
        conversation['_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname'] === originalAssociation?.regardingType) ||
        regardingIs(conversation, row.incidentid, 'incident'));
    let stage = 'configuration';
    try {
      if (!getDeployment().features.nativeVoice) {
        throw new ReportError('MODULE_DISABLED', 'Native voice is disabled; the case was not linked to a native conversation.', 503);
      }
      const allowed = nativeWorkstreams();
      if (!allowed.length) {
        throw new ReportError('NATIVE_WORKSTREAM_UNCONFIGURED', 'Native linking requires explicit approved workstream IDs.', 503);
      }
      let phoneVerified = false;
      for (let attempt = 0; attempt < 3; attempt++) {
        stage = 'validate';
        const conversation = schema.parse(await this.crm.request('GET', conversationPath));
        if (!allowed.includes(conversation._msdyn_liveworkstreamid_value) ||
          (conversation._msdyn_customer_value && conversation._msdyn_customer_value !== customer.contactid)) {
          throw new ReportError('CONVERSATION_MISMATCH', 'The native workstream/customer association does not match.', 409);
        }
        const association = {
          caseId: conversation._msdyn_issueid_value, regardingId: conversation._regardingobjectid_value,
          regardingType: conversation['_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname'],
        };
        if (originalAssociation && !compatibleAssociation(conversation)) {
          throw new ReportError('CONVERSATION_LINK_CHANGED', 'The conversation association changed during linking; the newer selection was preserved.', 409);
        }
        originalAssociation ??= association;
        if (conversation._regardingobjectid_value &&
          !regardingIs(conversation, customer.contactid, 'contact') &&
          !(conversation._msdyn_issueid_value && regardingIs(conversation, conversation._msdyn_issueid_value, 'incident'))) {
          throw new ReportError('CONVERSATION_MISMATCH', 'The conversation is regarding an unrelated record; it was not overwritten.', 409);
        }
        if (!phoneVerified) {
          const phones = z.object({ value: z.array(z.object({ msdyn_fromphone: z.string() })) }).parse(
            await this.crm.request('GET', query('msdyn_ocphonecallengagementctxes', {
              '$select': 'msdyn_fromphone', '$filter': `_msdyn_liveworkitemid_value eq ${conversationId}`, '$top': '2',
            })),
          );
          if (phones.value.length !== 1 || phones.value[0].msdyn_fromphone !== customer.mobilephone) {
            throw new ReportError('CONVERSATION_MISMATCH', 'The native caller does not match the selected customer.', 409);
          }
          phoneVerified = true;
        }
        if (isLinked(conversation)) {
          return { status: 'linked', verifiedAt: new Date().toISOString(), alreadyLinked: true };
        }
        if (![1, 2].includes(conversation.statuscode)) {
          throw new ReportError('CONVERSATION_NOT_ACTIVE', 'The conversation ended or entered wrap-up; historical links were not changed.', 409);
        }
        stage = 'patch';
        try {
          await this.crm.request('PATCH', `msdyn_ocliveworkitems(${conversationId})`, {
            'msdyn_customer_msdyn_ocliveworkitem_contact@odata.bind': `/contacts(${customer.contactid})`,
            'msdyn_IssueId_msdyn_ocliveworkitem@odata.bind': `/incidents(${row.incidentid})`,
            'regardingobjectid_incident_msdyn_ocliveworkitem@odata.bind': `/incidents(${row.incidentid})`,
          }, { 'If-Match': conversation['@odata.etag'] });
        } catch (error) {
          if (error instanceof DataverseError && error.status === 412 && attempt < 2) {
            console.warn('Native case link version changed; revalidating', { conversationId, attempt: attempt + 1 });
            continue;
          }
          if (!uncertainWrite(error)) throw error;
          console.warn('Reconciling an uncertain native case link without repeating its write', { conversationId, code: error.code });
        }
        stage = 'readback';
        await this.readBack(async timeoutMs => {
          const verified = schema.parse(await this.crm.request('GET', conversationPath, undefined, undefined, timeoutMs));
          if (!allowed.includes(verified._msdyn_liveworkstreamid_value) ||
            (verified._msdyn_customer_value && verified._msdyn_customer_value !== customer.contactid)) {
            throw new ReportError('CONVERSATION_MISMATCH', 'The native workstream/customer changed during link verification.', 409);
          }
          if (!compatibleAssociation(verified)) {
            throw new ReportError('CONVERSATION_LINK_CHANGED', 'The conversation association changed during readback; it was not overwritten.', 409);
          }
          return verified;
        }, verified => !isLinked(verified));
        console.info('Native case link verified', { conversationId, caseId: row.incidentid });
        return { status: 'linked', verifiedAt: new Date().toISOString(), alreadyLinked: false };
      }
      throw new ReportError('CONVERSATION_LINK_UNCONFIRMED', 'The bounded case-link attempts did not complete.', 409);
    } catch (error) {
      if (!(error instanceof DataverseError) && !(error instanceof ReportError) && !(error instanceof z.ZodError)) throw error;
      const code = error instanceof z.ZodError ? 'CRM_RESPONSE_INVALID' : error.code;
      const httpStatus = error instanceof z.ZodError ? 502 : error.status;
      console.error('Native case link not confirmed', {
        conversationId, caseId: row.incidentid, stage, code, httpStatus,
        nativeCode: error instanceof DataverseError ? error.nativeCode : undefined,
      });
      return {
        status: 'failed', error: code, httpStatus, stage,
        outcome: stage !== 'configuration' && uncertainWrite(error) ? 'unknown' : 'rejected',
        message: 'The case operation succeeded, but linking it to the native conversation was not confirmed. An unknown write outcome is not proof that the association is absent.',
      };
    }
  }

  async execute(request: Request) {
    requireModule('crm');
    request = caseRequestSchema.parse(request);
    const deadline = Date.now() + OPERATION_WINDOW_MS;
    const transport: DataverseTransport = {
      request: (method, path, body, headers, timeoutMs = method === 'PATCH' ? 10000 : 4000) => {
        const remaining = deadline - Date.now();
        if (remaining <= 0) {
          throw new DataverseError(504, 'CRM_OUTCOME_UNKNOWN', 'The operation confirmation deadline expired. Reconcile the same operation before another write.');
        }
        return this.crm.request(method, path, body, headers, Math.min(timeoutMs, remaining));
      },
    };
    return new CaseService(transport, deadline).executeRequest(request);
  }

  private async executeRequest(request: Request) {
    if (request.action === 'resolve_location') {
      return { success: true, location: { city: getDeployment().business.city, ...request.location }, jurisdictionVerified: false };
    }
    const customer = await this.customer(request.actorId);
    if (request.action === 'customer_context') {
      return {
        success: true, source: 'dynamics', identityVerified: false, dataScope: 'synthetic_profiles_only',
        customer: customer ? {
          id: customer.contactid, name: customer.fullname, firstName: customer.firstname,
          email: customer.emailaddress1, phone: customer.mobilephone, context: customer.description,
          preferredLanguage: customer.moacc_preferredlanguage,
          account: customer.parentcustomerid_account ?? null,
        } : null,
        ...(customer ? await this.cases(customer) : { cases: [], casesTruncated: false }),
      };
    }
    if (!customer) throw new ReportError('CUSTOMER_NOT_FOUND', 'No eligible Dynamics customer is linked. Ask a representative; do not create an anonymous or substitute case.', 404);
    if (request.action === 'list_cases') return { success: true, source: 'dynamics', ...await this.cases(customer) };
    let row: Case;
    if (request.action === 'create_report') {
      const key = hash(`${request.actorId}:${request.operationId}`);
      const id = operationGuid(key);
      const payloadHash = hash(JSON.stringify({ category: request.category, description: request.description, location: request.location }));
      row = await this.createOnly(`incidents(${id})`, {
        title: `${request.category}: ${request.description.slice(0, 160)}`,
        description: request.description, 'customerid_contact@odata.bind': `/contacts(${customer.contactid})`,
        moacc_requestkey: key, moacc_payloadhash: payloadHash, moacc_category: request.category,
        moacc_district: request.location.district, moacc_street: request.location.street,
        moacc_landmark: request.location.landmark,
      }, async timeoutMs => caseRow.parse(await this.crm.request(
        'GET', query(`incidents(${id})`, { '$select': caseFields }), undefined, undefined, timeoutMs,
      )));
      if (row._customerid_value !== customer.contactid || row.moacc_payloadhash !== payloadHash) {
        throw new ReportError('IDEMPOTENCY_CONFLICT', 'This operation already recorded different case details.', 409);
      }
    } else {
      row = await this.read(request.reference, customer);
    }
    if (request.action === 'add_followup') {
      const id = operationGuid(`${row.incidentid}:${request.operationId}`);
      const note = await this.createOnly(`annotations(${id})`, {
        subject: 'Customer update', notetext: request.note,
        'objectid_incident@odata.bind': `/incidents(${row.incidentid})`,
      }, async timeoutMs => z.object({ notetext: z.string(), _objectid_value: z.guid() }).parse(
        await this.crm.request('GET', query(`annotations(${id})`, { '$select': 'notetext,_objectid_value' }),
          undefined, undefined, timeoutMs),
      ));
      if (note.notetext !== request.note || note._objectid_value !== row.incidentid) {
        throw new ReportError('IDEMPOTENCY_CONFLICT', 'This operation already recorded different follow-up details.', 409);
      }
    }
    const notes = z.object({ value: z.array(z.object({ notetext: z.string().nullable(), createdon: z.string() })) }).parse(
      await this.crm.request('GET', query('annotations', {
        '$select': 'notetext,createdon', '$filter': `_objectid_value eq ${row.incidentid} and subject eq 'Customer update' and isdocument eq false`,
        '$orderby': 'createdon desc', '$top': '3',
      })),
    );
    return {
      ...this.receipt(row),
      recentActivities: notes.value.map(note => ({ type: 'followup', note: note.notetext, timestamp: note.createdon })),
      conversationLink: await this.link(row, customer, request),
    };
  }
}
