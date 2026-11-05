import type { OAuthHelpers } from '@cloudflare/workers-oauth-provider';

/** Bindings and secrets of the gaohe-probe Worker (see wrangler.jsonc). */
export interface Env {
  DB: D1Database;
  /** Required by @cloudflare/workers-oauth-provider (clients, grants, tokens, consent/upstream state). */
  OAUTH_KV: KVNamespace;
  /** Injected at runtime by OAuthProvider before the default handler runs. */
  OAUTH_PROVIDER: OAuthHelpers;
  // Secrets (wrangler secret put …)
  GITHUB_CLIENT_ID: string;
  GITHUB_CLIENT_SECRET: string;
  IP_HMAC_KEY: string;
  REPORT_TOKEN: string;
  /** P0 allowlist of GitHub numeric user ids, comma separated. Empty = nobody may sign in. */
  PROBE_ALLOWED_GITHUB_IDS: string;
}

/** Props stored (encrypted) in the grant and delivered to /mcp as ctx.props. */
export interface ProbeProps extends Record<string, unknown> {
  githubId: number;
  githubCreatedAt: string;
  oauthIpIsAnthropic: boolean;
  oauthIpPrefixHmac: string;
}
