import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  retries: 0,
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:38471', viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' },
  webServer: [{
    command: 'node node_modules/next/dist/bin/next dev --webpack --hostname 127.0.0.1 --port 38471',
    url: 'http://127.0.0.1:38471',
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10000 },
    timeout: 120000,
    env: {
      NEXT_PUBLIC_API_URL: 'http://127.0.0.1:38471/backend',
      NEXT_PUBLIC_VOICE_URL: 'http://127.0.0.1:38471/voice-api',
      NEXT_TELEMETRY_DISABLED: '1',
    },
  }, {
    command: 'node tests/staged-server.mjs',
    url: 'http://127.0.0.1:38472',
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10000 },
    timeout: 120000,
    env: {
      NEXT_PUBLIC_API_URL: 'http://127.0.0.1:38472/backend',
      NEXT_PUBLIC_VOICE_URL: 'http://127.0.0.1:38472/voice-api',
      NEXT_TELEMETRY_DISABLED: '1',
    },
  }, {
    command: 'cd ../agent_console && node node_modules/next/dist/bin/next dev --hostname 127.0.0.1 --port 38473',
    url: 'http://127.0.0.1:38473',
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10000 },
    timeout: 120000,
    env: { NEXT_PUBLIC_API_URL: 'http://127.0.0.1:38473/agent-backend', NEXT_TELEMETRY_DISABLED: '1' },
  }, {
    command: 'node tests/staged-server.mjs agent',
    url: 'http://127.0.0.1:38474',
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10000 },
    timeout: 120000,
    env: { NEXT_PUBLIC_API_URL: 'http://127.0.0.1:38474/agent-backend', NEXT_TELEMETRY_DISABLED: '1' },
  }],
});
