// Unit tests: plain Node environment (Node 22 provides crypto.subtle, TextEncoder, String.normalize).
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['test/unit/**/*.test.ts'],
  },
});
