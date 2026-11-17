/**
 * Vitest setup file
 * 
 * Sets up test environment with appropriate skip conditions
 * for tests that require Azure services.
 */
import { vi } from 'vitest';

// Existing backend tests explicitly exercise enabled modules, never production content.
vi.mock('@/config/deployment.json', async (importOriginal) => {
  const { default: base } = await importOriginal<{ default: typeof import('./src/config/deployment.json') }>();
  return {
    default: {
      ...base,
      features: Object.fromEntries(Object.keys(base.features).map(feature => [feature, true])),
      business: { caseCategories: ['general'], locationRequired: true, city: 'Test city' },
      knowledge: { allowedDomains: ['docs.example.test'] },
      emailPolicy: { allowedRecipients: [], allowedDomains: ['example.test'] },
    },
  };
});

// Check if we're in a CI/Azure environment with real credentials
const hasAzureCredentials = !!(
  process.env.COSMOS_DB_ENDPOINT &&
  process.env.COSMOS_DB_KEY &&
  process.env.AZURE_OPENAI_ENDPOINT &&
  process.env.AZURE_OPENAI_API_KEY
);

// Export for use in tests
export const SKIP_AZURE_TESTS = !hasAzureCredentials;

// Log status
if (!hasAzureCredentials) {
  console.log('\n⚠️  Azure credentials not configured - integration tests will be skipped');
  console.log('   Set COSMOS_DB_ENDPOINT, COSMOS_DB_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY to run integration tests\n');
}
