/**
 * Default (non-API) handler for OAuthProvider: consent page, GitHub upstream sign-in, callback,
 * and the guarded /probe/report route.
 *
 * Flow (per @cloudflare/workers-oauth-provider docs/upstream-sign-in.md + consent-page.md):
 *   GET  /authorize  parseAuthRequest → describeConsent → beginConsent → minimal consent page
 *                    (shows client name, CIMD domain if any, and the actual redirect host — spec §9.1)
 *   POST /authorize  approveConsent → beginUpstream (state stored in OAUTH_KV with a 10-minute TTL,
 *                    single use, bound to this browser by a __Host- cookie) → 302 to GitHub
 *   GET  /callback   finishUpstream (validates state + cookie) → exchange code → GET /user
 *                    → allowlist (PROBE_ALLOWED_GITHUB_IDS) → D1 writes (skipped for accounts
 *                    younger than 30 days, spec §9.1) → completeAuthorization(props) → 302 back
 *                    to the MCP client. Once the state is consumed, every failure is returned to
 *                    the MCP client as an OAuth error redirect, never as a bare 500.
 */
import {
  AuthorizationError,
  CimdFetchError,
  authorizationErrorRedirect,
  type ConsentDescription,
} from '@cloudflare/workers-oauth-provider';
import { isAllowedGitHubId, isTooNew } from './access';
import { isAnthropicEgress } from './anthropicEgress';
import type { Env, ProbeProps } from './env';
import {
  buildGitHubAuthorizeUrl,
  exchangeGitHubCode,
  fetchGitHubUser,
  newCodeVerifier,
  s256Challenge,
} from './github';
import { assertIpHmacKey, ipPrefixHmac, MisconfiguredError, timingSafeEqualStr } from './ip';
import { recordMetric } from './metrics';
import { loadReport } from './report';

const escapeHtml = (v: string) => v.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function consentPage(d: ConsentDescription, handle: string): string {
  const name = escapeHtml(d.clientName);
  const origin = d.clientDomain
    ? `發布者網域：<strong>${escapeHtml(d.clientDomain)}</strong>`
    : '此應用程式為自行註冊，名稱未經驗證。';
  return `<!doctype html>
<html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GaoHe probe 授權</title>
<body style="font-family:system-ui,sans-serif;max-width:36rem;margin:2rem auto;padding:0 1rem">
<h1>允許「${name}」使用 GaoHe probe？</h1>
<p>${origin}</p>
<p>授權結果將送往：<strong>${escapeHtml(d.redirectHost)}</strong></p>
${d.redirectIsLoopback ? '<p><strong>注意：這會把存取權交給你電腦上的程式。</strong>只有你剛從該程式開始登入時才繼續。</p>' : ''}
<p>下一步會以 GitHub 登入（不要求任何 GitHub 權限範圍，只讀取公開的使用者編號與帳號建立時間）。</p>
<form method="post">
  <input type="hidden" name="handle" value="${escapeHtml(handle)}">
  <button name="decision" value="approve">允許並以 GitHub 登入</button>
  <button name="decision" value="deny">拒絕</button>
</form></body></html>`;
}

function text(status: number, body: string): Response {
  return new Response(body, { status, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
}

function callbackUrl(request: Request): string {
  return new URL('/callback', request.url).toString();
}

async function handleAuthorizeGet(request: Request, env: Env): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  const authReq = await oauth.parseAuthRequest(request);
  const details = await oauth.describeConsent(authReq); // first: a failed lookup leaves nothing in KV
  const consent = await oauth.beginConsent(authReq);
  consent.headers.set('Content-Type', 'text/html; charset=utf-8');
  return new Response(consentPage(details, consent.handle), { headers: consent.headers });
}

async function handleAuthorizePost(request: Request, env: Env): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  const form = await request.formData();
  const handle = String(form.get('handle') ?? '');
  if (form.get('decision') !== 'approve') {
    const denied = await oauth.denyConsent(request, handle);
    return new Response(null, { status: 302, headers: denied.headers });
  }
  const approved = await oauth.approveConsent(request, handle);
  const verifier = newCodeVerifier();
  const { state, headers } = await oauth.beginUpstream(approved.request, {
    data: { verifier },
    headers: approved.headers,
  });
  headers.set(
    'Location',
    buildGitHubAuthorizeUrl({
      clientId: env.GITHUB_CLIENT_ID,
      redirectUri: callbackUrl(request),
      state,
      codeChallenge: await s256Challenge(verifier),
    }),
  );
  return new Response(null, { status: 302, headers });
}

async function handleCallback(request: Request, env: Env): Promise<Response> {
  // Checked before finishUpstream so a misconfigured deployment does not consume the state.
  try {
    assertIpHmacKey(env.IP_HMAC_KEY);
  } catch (e) {
    if (e instanceof MisconfiguredError) return text(500, `Server misconfigured: ${e.message}\n`);
    throw e;
  }
  const oauth = env.OAUTH_PROVIDER;
  const { request: original, data, headers } = await oauth.finishUpstream<{ verifier: string }>(request);
  const redirectError = (code: 'access_denied' | 'server_error', description?: string) => {
    headers.set('Location', authorizationErrorRedirect(original, code, description));
    return new Response(null, { status: 302, headers });
  };
  const params = new URL(request.url).searchParams;
  const code = params.get('code');
  if (params.get('error') || !code) return redirectError('access_denied');

  let user: { id: number; created_at: string };
  try {
    const githubToken = await exchangeGitHubCode({
      clientId: env.GITHUB_CLIENT_ID,
      clientSecret: env.GITHUB_CLIENT_SECRET,
      code,
      redirectUri: callbackUrl(request),
      codeVerifier: data.verifier,
    });
    user = await fetchGitHubUser(githubToken); // the GitHub token goes out of scope here; never stored
  } catch (e) {
    console.error('github sign-in failed', e instanceof Error ? e.message : 'unknown error');
    return redirectError('server_error', 'GitHub sign-in failed');
  }

  // P0 allowlist: refuse before anything is written for this account.
  if (!isAllowedGitHubId(env.PROBE_ALLOWED_GITHUB_IDS, user.id)) {
    return redirectError('access_denied', 'This GitHub account is not enabled for the GaoHe probe');
  }

  // OAuth browser IP: the clustering signal of spec §9.4 (HMAC'd prefix only).
  const ip = request.headers.get('CF-Connecting-IP') ?? '';
  const props: ProbeProps = {
    githubId: user.id,
    githubCreatedAt: user.created_at,
    oauthIpIsAnthropic: isAnthropicEgress(ip),
    oauthIpPrefixHmac: await ipPrefixHmac(env.IP_HMAC_KEY, ip),
  };
  // Accounts younger than 30 days may sign in and use the tools, but nothing is written (spec §9.1).
  if (!isTooNew(user.created_at)) {
    try {
      const now = new Date().toISOString();
      const results = await env.DB.batch([
        env.DB.prepare(
          `INSERT INTO accounts (github_id, github_created_at, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(github_id) DO UPDATE SET last_seen_at = excluded.last_seen_at, github_created_at = excluded.github_created_at`,
        ).bind(user.id, user.created_at, now, now),
        env.DB.prepare(
          'INSERT INTO ip_observations (account_id, source, is_anthropic, prefix_hmac, at) VALUES (?, ?, ?, ?, ?)',
        ).bind(user.id, 'oauth', props.oauthIpIsAnthropic ? 1 : 0, props.oauthIpPrefixHmac, now),
      ]);
      // Item 5: the callback's own D1 writes are measured too (not counted toward the daily limit).
      await recordMetric(env, 'callback', user.id, results);
    } catch (e) {
      console.error('callback D1 write failed', e instanceof Error ? e.message : 'unknown error');
      return redirectError('server_error');
    }
  }

  const { redirectTo } = await oauth.completeAuthorization({
    request: original,
    userId: String(user.id),
    metadata: { label: `github:${user.id}` },
    scope: original.scope,
    props,
  });
  headers.set('Location', redirectTo);
  return new Response(null, { status: 302, headers });
}

async function handleReport(request: Request, env: Env): Promise<Response> {
  const auth = request.headers.get('Authorization') ?? '';
  const m = /^Bearer\s+(.+)$/i.exec(auth);
  if (!env.REPORT_TOKEN || !m || !timingSafeEqualStr(m[1]!.trim(), env.REPORT_TOKEN)) {
    return new Response(JSON.stringify({ error: 'unauthorized' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json', 'WWW-Authenticate': 'Bearer realm="gaohe-probe-report"' },
    });
  }
  return Response.json(await loadReport(env.DB), { headers: { 'Cache-Control': 'no-store' } });
}

export const authHandler: ExportedHandler<Env> = {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    try {
      if (url.pathname === '/authorize' && request.method === 'GET') return await handleAuthorizeGet(request, env);
      if (url.pathname === '/authorize' && request.method === 'POST') return await handleAuthorizePost(request, env);
      if (url.pathname === '/callback' && request.method === 'GET') return await handleCallback(request, env);
      if (url.pathname === '/probe/report' && request.method === 'GET') return await handleReport(request, env);
      if (url.pathname === '/' && request.method === 'GET') {
        return text(200, 'gaohe-probe: GaoHe P0 feasibility probe. MCP endpoint: /mcp\n');
      }
      return text(404, 'Not found\n');
    } catch (error) {
      if (error instanceof AuthorizationError && error.redirectTo) return Response.redirect(error.redirectTo, 302);
      if (error instanceof AuthorizationError) return text(400, error.description);
      if (error instanceof CimdFetchError) return text(400, 'This app could not be verified.');
      throw error;
    }
  },
};
