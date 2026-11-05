/**
 * gaohe-probe Worker entry. Only the default export lives here (workerd requires every named
 * export of the main module to be a handler). One OAuthProvider per request origin, cached for the
 * isolate's lifetime in a small bounded cache; see src/provider.ts.
 */
import type { OAuthProvider } from '@cloudflare/workers-oauth-provider';
import type { Env } from './env';
import { buildProvider } from './provider';
import { purgeOldIpObservations } from './retention';

/** workers.dev + a custom domain + wrangler dev need at most a handful of origins. */
const MAX_CACHED_PROVIDERS = 4;
const providers = new Map<string, OAuthProvider<Env>>();

function providerFor(origin: string): OAuthProvider<Env> {
  let p = providers.get(origin);
  if (p) {
    // Refresh recency (Map keeps insertion order).
    providers.delete(origin);
  } else {
    p = buildProvider(origin);
    if (providers.size >= MAX_CACHED_PROVIDERS) providers.delete(providers.keys().next().value!);
  }
  providers.set(origin, p);
  return p;
}

export default {
  fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    return providerFor(new URL(request.url).origin).fetch(request, env, ctx);
  },
  /** Daily cron (wrangler.jsonc "triggers"): 90-day retention for ip_observations (spec §11). */
  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(
      purgeOldIpObservations(env.DB).then((n) => console.log(`retention: deleted ${n} ip_observations rows`)),
    );
  },
} satisfies ExportedHandler<Env>;
