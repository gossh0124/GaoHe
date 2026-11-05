/**
 * OAuthProvider construction. Kept out of src/index.ts because workerd rejects any named export of
 * the main module that is not a handler (e.g. a string constant).
 *
 * OAuthProvider needs a canonical HTTPS `resourceMetadata.resource` at construction time
 * (workers-oauth-provider 1.2.x). To work unchanged on workers.dev, a custom domain and
 * `wrangler dev` (http://localhost is accepted as loopback), index.ts builds one provider per
 * request origin. The resource is `<origin>/mcp`, i.e. exactly the connector URL the user pastes
 * into claude.ai (spec §9.1).
 */
import { OAuthProvider } from '@cloudflare/workers-oauth-provider';
import { createMcpHandler } from 'agents/mcp/server';
import { authHandler } from './auth';
import type { Env } from './env';
import { createProbeServer } from './probe';

export const MCP_ROUTE = '/mcp';

/** /mcp handler: ctx.props / ctx.auth are set by OAuthProvider after token validation;
 *  agents' createMcpHandler reads them and exposes props through getMcpAuthContext(). */
const mcpApiHandler: ExportedHandlerWithFetchOnly = {
  fetch(request, env, ctx) {
    const handler = createMcpHandler(() => createProbeServer(env, request), { route: MCP_ROUTE });
    return handler(request, env, ctx);
  },
};

type ExportedHandlerWithFetchOnly = {
  fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response>;
};

export function buildProvider(origin: string): OAuthProvider<Env> {
  return new OAuthProvider<Env>({
    apiRoute: MCP_ROUTE,
    apiHandler: mcpApiHandler,
    defaultHandler: authHandler,
    authorizeEndpoint: '/authorize',
    tokenEndpoint: '/token',
    clientRegistrationEndpoint: '/register',
    // CIMD is supported by 1.2.4; it also needs the global_fetch_strictly_public compat flag
    // (set in wrangler.jsonc). DCR stays enabled for clients that do not use CIMD.
    clientIdMetadataDocumentEnabled: true,
    accessTokenTTL: 3600,
    refreshTokenTTL: 30 * 24 * 3600,
    resourceMetadata: {
      resource: `${origin}${MCP_ROUTE}`,
      resource_name: 'GaoHe probe (P0)',
      bearer_methods_supported: ['header'],
    },
  });
}
