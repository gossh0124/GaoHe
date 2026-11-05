import type { D1Migration } from 'cloudflare:test';
import type { Env as WorkerEnv } from '../../src/env';

declare global {
  namespace Cloudflare {
    interface GlobalProps {
      mainModule: typeof import('../../src/index');
    }
    interface Env extends Omit<WorkerEnv, 'OAUTH_PROVIDER'> {
      TEST_MIGRATIONS: D1Migration[];
    }
  }
}
export {};
