import { z } from 'zod';
import packagedDeployment from '@/config/deployment.json';

const domain = z.string().regex(/^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/);
const unique = <T>(values: T[]) => new Set(values).size === values.length;
const domains = z.array(domain).refine(unique, 'Domains must be unique.');
const text = z.string().trim().min(1);
export const deploymentSchema = z.object({
  schemaVersion: z.literal(1),
  organization: z.object({
    name: text,
    assistantName: text,
    defaultLanguage: z.enum(['en', 'ar']),
    timeZone: text.refine(value => {
      try { new Intl.DateTimeFormat('en', { timeZone: value }); return true; } catch { return false; }
    }, 'Use a valid IANA time zone.'),
    greeting: z.object({ en: text, ar: text }).strict(),
  }).strict(),
  features: z.object({
    crm: z.boolean(),
    knowledge: z.boolean(),
    email: z.boolean(),
    agentAssist: z.boolean(),
    applicationTelephony: z.boolean(),
    browserVoice: z.boolean(),
    nativeVoice: z.boolean(),
    agentConsole: z.boolean(),
  }).strict(),
  business: z.object({
    caseCategories: z.array(z.string().regex(/^[a-z][a-z0-9_-]{0,31}$/)).min(1).max(30).refine(unique, 'Case categories must be unique.'),
    locationRequired: z.boolean(),
    city: z.string().trim(),
  }).strict(),
  knowledge: z.object({ allowedDomains: domains }).strict(),
  emailPolicy: z.object({
    allowedRecipients: z.array(z.email().toLowerCase()).refine(unique, 'Recipients must be unique.'),
    allowedDomains: domains,
  }).strict(),
  phonePolicy: z.object({
    allowedOutboundNumbers: z.array(z.string().regex(/^\+[1-9][0-9]{6,14}$/)).max(20).refine(unique, 'Phone destinations must be unique.'),
  }).strict(),
}).strict();

export type Deployment = z.infer<typeof deploymentSchema>;
export type Feature = keyof Deployment['features'];

export function parseDeployment(value: unknown): Deployment {
  const parsed = deploymentSchema.safeParse(value);
  if (!parsed.success) {
    throw new Error(`Invalid deployment.json: ${parsed.error.issues.map(issue => `${issue.path.join('.')}: ${issue.message}`).join('; ')}`);
  }
  return parsed.data;
}

const deployment = parseDeployment(packagedDeployment);
export function getDeployment(): Deployment {
  return deployment;
}

export class ModuleDisabledError extends Error {
  readonly code = 'MODULE_DISABLED';
  readonly status = 503;
  constructor(feature: Feature) { super(`${feature} module is disabled.`); }
}

export function requireModule(feature: Feature): void {
  if (!getDeployment().features[feature]) throw new ModuleDisabledError(feature);
}
