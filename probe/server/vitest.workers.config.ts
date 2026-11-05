// Integration tests: run inside workerd via @cloudflare/vitest-plugin, using wrangler.jsonc bindings.
import path from 'node:path';
import { cloudflareTest, readD1Migrations } from '@cloudflare/vitest-plugin';
import { defineConfig } from 'vitest/config';

export default defineConfig(async () => {
  const migrations = await readD1Migrations(path.join(import.meta.dirname, 'migrations'));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: './wrangler.jsonc' },
        miniflare: {
          bindings: {
            TEST_MIGRATIONS: migrations,
            GITHUB_CLIENT_ID: 'test-client-id',
            GITHUB_CLIENT_SECRET: 'test-client-secret',
            IP_HMAC_KEY: 'test-ip-hmac-key-0123456789abcdef0123456789',
            REPORT_TOKEN: 'test-report-token',
            // 424242 main, 515151 too new, 616161 daily-limit; 999 (outsider) is deliberately absent.
            PROBE_ALLOWED_GITHUB_IDS: '424242, 515151, 616161',
          },
        },
      }),
    ],
    test: {
      include: ['test/integration/**/*.test.ts'],
      setupFiles: ['./test/integration/apply-migrations.ts'],
    },
  };
});
