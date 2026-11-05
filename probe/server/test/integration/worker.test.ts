/**
 * Boots the real Worker in workerd (Miniflare) and drives:
 *   discovery metadata → 401 challenge → DCR → consent → GitHub (mocked) → callback → token
 *   → MCP tools over streamable HTTP → Cloudflare D1 rows → /probe/report.
 */
import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import { env, exports } from 'cloudflare:workers';
import { purgeOldIpObservations } from '../../src/retention';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const ORIGIN = 'https://gaohe-probe.test';
const RESOURCE = `${ORIGIN}/mcp`;
const CLIENT_REDIRECT = 'https://client.example/oauth/callback';
const ANTHROPIC_IP = '160.79.104.10';
const BROWSER_IP = '203.0.113.7';

function worker(path: string, init?: RequestInit): Promise<Response> {
  return exports.default.fetch(new Request(`${ORIGIN}${path}`, { redirect: 'manual', ...init }));
}

/** Minimal cookie jar: name → value from Set-Cookie headers. */
class Jar {
  private c = new Map<string, string>();
  absorb(res: Response) {
    for (const sc of res.headers.getSetCookie()) {
      const [pair] = sc.split(';');
      const i = pair!.indexOf('=');
      const name = pair!.slice(0, i).trim();
      const value = pair!.slice(i + 1).trim();
      if (/max-age=0|expires=thu, 01 jan 1970/i.test(sc) || value === '') this.c.delete(name);
      else this.c.set(name, value);
    }
  }
  header(): string {
    return [...this.c.entries()].map(([k, v]) => `${k}=${v}`).join('; ');
  }
}

function b64url(bytes: Uint8Array): string {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Parse a JSON or SSE MCP response into the JSON-RPC message carrying `id`. */
async function rpcResult(res: Response): Promise<any> {
  const ct = res.headers.get('content-type') ?? '';
  const text = await res.text();
  if (ct.includes('text/event-stream')) {
    const msgs = text
      .split('\n')
      .filter((l) => l.startsWith('data:'))
      .map((l) => JSON.parse(l.slice(5).trim()));
    return msgs.find((m) => 'result' in m || 'error' in m);
  }
  return JSON.parse(text);
}

let accessToken = '';
const githubCalls: string[] = [];
const realFetch = globalThis.fetch;

/** Mocked GitHub users, keyed by the authorization code the test sends to /callback. */
const DAY = 86_400_000;
const GH_USERS: Record<string, { id: number; created_at: string }> = {
  'gh-code-1': { id: 424242, created_at: '2020-01-01T00:00:00Z' },
  'gh-code-new': { id: 515151, created_at: new Date(Date.now() - 5 * DAY).toISOString() }, // 5 days old
  'gh-code-limit': { id: 616161, created_at: '2019-06-01T00:00:00Z' },
  'gh-code-outsider': { id: 999, created_at: '2015-01-01T00:00:00Z' }, // not on the allowlist
};

beforeAll(() => {
  // The Worker runs in the same isolate as the tests, so this stubs its outbound GitHub calls.
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url.startsWith('https://github.com/login/oauth/access_token')) {
      githubCalls.push(url);
      const body = new URLSearchParams(String(init?.body ?? ''));
      const code = body.get('code') ?? '';
      if (code === 'gh-code-down') return new Response('upstream down', { status: 502 });
      if (!GH_USERS[code] || !body.get('code_verifier')) return Response.json({ error: 'bad_verification_code' });
      return Response.json({ access_token: `gho_${code}`, token_type: 'bearer', scope: '' });
    }
    if (url.startsWith('https://api.github.com/user')) {
      githubCalls.push(url);
      const auth = new Headers(init?.headers).get('Authorization') ?? '';
      const user = GH_USERS[auth.replace(/^Bearer gho_/, '')];
      if (!user) return new Response('{"message":"Bad credentials"}', { status: 401 });
      return Response.json({ ...user, login: `u${user.id}` });
    }
    return realFetch(input, init);
  });
});

/**
 * Full OAuth round trip for one GitHub code: DCR → consent → GitHub (mocked) → /callback.
 * Returns the /callback response and, when it redirected back with a code, the access token.
 */
async function signIn(githubCode: string, ip = BROWSER_IP): Promise<{ callback: Response; accessToken: string | null }> {
  const reg = await worker('/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      client_name: `client-${githubCode}`,
      redirect_uris: [CLIENT_REDIRECT],
      token_endpoint_auth_method: 'none',
      grant_types: ['authorization_code', 'refresh_token'],
      response_types: ['code'],
    }),
  });
  const clientId: string = ((await reg.json()) as any).client_id;
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))));
  const q = new URLSearchParams({
    response_type: 'code',
    client_id: clientId,
    redirect_uri: CLIENT_REDIRECT,
    state: `st-${githubCode}`,
    code_challenge: challenge,
    code_challenge_method: 'S256',
    resource: RESOURCE,
  });
  const jar = new Jar();
  const page = await worker(`/authorize?${q}`, { headers: { 'CF-Connecting-IP': ip } });
  jar.absorb(page);
  const handle = /name="handle" value="([^"]+)"/.exec(await page.text())![1]!;
  const approve = await worker('/authorize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Cookie: jar.header(), 'CF-Connecting-IP': ip },
    body: new URLSearchParams({ handle, decision: 'approve' }),
  });
  jar.absorb(approve);
  const state = new URL(approve.headers.get('Location')!).searchParams.get('state')!;
  const callback = await worker(`/callback?code=${githubCode}&state=${encodeURIComponent(state)}`, {
    headers: { Cookie: jar.header(), 'CF-Connecting-IP': ip },
  });
  const back = callback.headers.get('Location') ? new URL(callback.headers.get('Location')!) : null;
  const code = back?.searchParams.get('code');
  if (!code) return { callback, accessToken: null };
  const tok = await worker('/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      redirect_uri: CLIENT_REDIRECT,
      client_id: clientId,
      code_verifier: verifier,
      resource: RESOURCE,
    }),
  });
  return { callback, accessToken: ((await tok.json()) as any).access_token };
}

let rpcSeq = 1000;
async function mcpAs(token: string, method: string, params: unknown, ip = ANTHROPIC_IP): Promise<any> {
  const res = await worker('/mcp', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      'MCP-Protocol-Version': '2025-06-18',
      'CF-Connecting-IP': ip,
    },
    body: JSON.stringify({ jsonrpc: '2.0', id: rpcSeq++, method, params }),
  });
  expect(res.status).toBe(200);
  return rpcResult(res);
}
afterAll(() => {
  vi.restoreAllMocks();
});

describe('main module', () => {
  it('exports only the default handler (workerd rejects non-handler named exports)', async () => {
    const main = await import('../../src/index');
    expect(Object.keys(main)).toEqual(['default']);
  });
});

describe('discovery and 401', () => {
  it('serves RFC 8414 authorization server metadata', async () => {
    const res = await worker('/.well-known/oauth-authorization-server');
    expect(res.status).toBe(200);
    const m: any = await res.json();
    expect(m.issuer).toBe(ORIGIN);
    expect(m.authorization_endpoint).toBe(`${ORIGIN}/authorize`);
    expect(m.token_endpoint).toBe(`${ORIGIN}/token`);
    expect(m.registration_endpoint).toBe(`${ORIGIN}/register`);
    expect(m.code_challenge_methods_supported).toContain('S256');
    expect(m.client_id_metadata_document_supported).toBe(true);
  });

  it('serves RFC 9728 protected resource metadata for /mcp', async () => {
    const res = await worker('/.well-known/oauth-protected-resource/mcp');
    expect(res.status).toBe(200);
    const m: any = await res.json();
    expect(m.resource).toBe(RESOURCE);
    expect(m.authorization_servers).toEqual([ORIGIN]);
  });

  it('records what the root /.well-known/oauth-protected-resource returns', async () => {
    const res = await worker('/.well-known/oauth-protected-resource');
    // Documented behaviour of 1.2.4 for a path-bearing resource; asserted so a change is noticed.
    console.log('root PRM status', res.status, (await res.text()).slice(0, 200));
    expect([200, 404]).toContain(res.status);
  });

  it('POST /mcp without a token → 401 with WWW-Authenticate resource_metadata', async () => {
    const res = await worker('/mcp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }),
    });
    expect(res.status).toBe(401);
    const www = res.headers.get('WWW-Authenticate') ?? '';
    expect(www).toMatch(/^Bearer /);
    expect(www).toContain(`resource_metadata="${ORIGIN}/.well-known/oauth-protected-resource/mcp"`);
  });

  it('GET /probe/report without the bearer token → 401', async () => {
    expect((await worker('/probe/report')).status).toBe(401);
    expect((await worker('/probe/report', { headers: { Authorization: 'Bearer wrong' } })).status).toBe(401);
  });
});

describe('OAuth: DCR → consent → GitHub (mocked) → token', () => {
  it('completes the flow and issues an access token for the /mcp resource', async () => {
    const reg = await worker('/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_name: 'Probe Test Client',
        redirect_uris: [CLIENT_REDIRECT],
        token_endpoint_auth_method: 'none',
        grant_types: ['authorization_code', 'refresh_token'],
        response_types: ['code'],
      }),
    });
    expect(reg.status).toBe(201);
    const client: any = await reg.json();
    const clientId: string = client.client_id;

    const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
    const challenge = b64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))));
    const q = new URLSearchParams({
      response_type: 'code',
      client_id: clientId,
      redirect_uri: CLIENT_REDIRECT,
      state: 'client-state-1',
      code_challenge: challenge,
      code_challenge_method: 'S256',
      resource: RESOURCE,
    });
    const jar = new Jar();
    const page = await worker(`/authorize?${q}`, { headers: { 'CF-Connecting-IP': BROWSER_IP } });
    expect(page.status).toBe(200);
    expect(page.headers.get('X-Frame-Options')).toBe('DENY');
    jar.absorb(page);
    const html = await page.text();
    expect(html).toContain('Probe Test Client');
    expect(html).toContain('client.example'); // actual redirect host shown (spec §9.1)
    const handle = /name="handle" value="([^"]+)"/.exec(html)![1]!;

    const approve = await worker('/authorize', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        Cookie: jar.header(),
        'CF-Connecting-IP': BROWSER_IP,
      },
      body: new URLSearchParams({ handle, decision: 'approve' }),
    });
    expect(approve.status).toBe(302);
    jar.absorb(approve);
    const gh = new URL(approve.headers.get('Location')!);
    expect(gh.origin + gh.pathname).toBe('https://github.com/login/oauth/authorize');
    expect(gh.searchParams.get('scope')).toBe('');
    expect(gh.searchParams.get('redirect_uri')).toBe(`${ORIGIN}/callback`);
    expect(gh.searchParams.get('code_challenge_method')).toBe('S256');
    const upstreamState = gh.searchParams.get('state')!;
    expect(upstreamState).toBeTruthy();

    // A callback without the browser-binding cookie must be refused (state is cookie-bound).
    const forged = await worker(`/callback?code=gh-code-1&state=${encodeURIComponent(upstreamState)}`);
    expect(forged.status).toBe(400);

    const cb = await worker(`/callback?code=gh-code-1&state=${encodeURIComponent(upstreamState)}`, {
      headers: { Cookie: jar.header(), 'CF-Connecting-IP': BROWSER_IP },
    });
    expect(cb.status).toBe(302);
    const back = new URL(cb.headers.get('Location')!);
    expect(back.origin + back.pathname).toBe(CLIENT_REDIRECT);
    expect(back.searchParams.get('state')).toBe('client-state-1');
    const code = back.searchParams.get('code')!;
    expect(code).toBeTruthy();
    expect(githubCalls.length).toBe(2);

    // State is single-use.
    const replay = await worker(`/callback?code=gh-code-1&state=${encodeURIComponent(upstreamState)}`, {
      headers: { Cookie: jar.header() },
    });
    expect(replay.status).toBe(400);

    const tok = await worker('/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'authorization_code',
        code,
        redirect_uri: CLIENT_REDIRECT,
        client_id: clientId,
        code_verifier: verifier,
        resource: RESOURCE,
      }),
    });
    expect(tok.status).toBe(200);
    const t: any = await tok.json();
    expect(t.token_type.toLowerCase()).toBe('bearer');
    accessToken = t.access_token;
    expect(accessToken).toBeTruthy();
    expect(t.refresh_token).toBeTruthy();

    const acct = await env.DB.prepare('SELECT * FROM accounts').all();
    expect(acct.results).toHaveLength(1);
    expect(acct.results[0]).toMatchObject({ github_id: 424242, github_created_at: '2020-01-01T00:00:00Z' });
    const ipo = await env.DB.prepare("SELECT * FROM ip_observations WHERE source = 'oauth'").all();
    expect(ipo.results).toHaveLength(1);
    expect(ipo.results[0]).toMatchObject({ account_id: 424242, is_anthropic: 0 });
    expect(String(ipo.results[0]!.prefix_hmac)).toMatch(/^[0-9a-f]{64}$/);
    // The GitHub token is never persisted.
    const kv = await env.OAUTH_KV.list();
    for (const k of kv.keys) expect(String(await env.OAUTH_KV.get(k.name))).not.toContain('gho_');
    // The callback's own D1 writes are measured (item 5).
    const cbm = await env.DB.prepare("SELECT * FROM metrics WHERE tool = 'callback'").all();
    expect(cbm.results).toHaveLength(1);
    expect(cbm.results[0]).toMatchObject({ account_id: 424242 });
    expect(Number(cbm.results[0]!.rows_written)).toBeGreaterThanOrEqual(2);
    expect(Number(cbm.results[0]!.metrics_rows_written)).toBeGreaterThanOrEqual(2);
  });
});

describe('MCP tools over streamable HTTP (stateless)', () => {
  let rpcId = 10;
  async function mcp(method: string, params: unknown, ip = ANTHROPIC_IP): Promise<any> {
    const res = await worker('/mcp', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
        Accept: 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2025-06-18',
        'CF-Connecting-IP': ip,
      },
      body: JSON.stringify({ jsonrpc: '2.0', id: rpcId++, method, params }),
    });
    expect(res.status).toBe(200);
    return rpcResult(res);
  }

  it('initialize + tools/list exposes the three probe tools with annotations', async () => {
    const init = await mcp('initialize', {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'test', version: '0' },
    });
    expect(init.result.serverInfo.name).toBe('gaohe-probe');
    const list = await mcp('tools/list', {});
    const tools = Object.fromEntries(list.result.tools.map((t: any) => [t.name, t]));
    expect(Object.keys(tools).sort()).toEqual(['probe_report_unavailable', 'probe_submit_snapshot', 'probe_whoami']);
    expect(tools.probe_whoami.annotations.readOnlyHint).toBe(false); // it writes ip_observations + metrics
    expect(tools.probe_whoami.annotations.destructiveHint).toBe(false);
    expect(tools.probe_submit_snapshot.annotations.readOnlyHint).toBe(false);
    expect(tools.probe_report_unavailable.inputSchema.properties.reason.enum).toContain('boilerplate_only');
  });

  it('probe_whoami returns the GitHub id and detects the Anthropic egress IP', async () => {
    const r = await mcp('tools/call', { name: 'probe_whoami', arguments: {} });
    const sc = r.result.structuredContent;
    expect(sc.githubId).toBe(424242);
    expect(sc.accountAgeDays).toBeGreaterThan(365);
    expect(sc.mcpRequestIpIsAnthropic).toBe(true);
    expect(sc.oauthIpWasAnthropic).toBe(false);
    const r2 = await mcp('tools/call', { name: 'probe_whoami', arguments: {} }, BROWSER_IP);
    expect(r2.result.structuredContent.mcpRequestIpIsAnthropic).toBe(false);
  });

  const BODY_A = [
    '（中央社記者王小明台北5日電）',
    '行政院今天召開記者會，說明明年度總預算的編列方向「不會改變」……',
    '立法院預計下週開始審查各部會的預算案內容。',
  ].join('\n');
  const BODY_B = [
    '​（中央社記者王小明台北5日電）',
    '',
    '行政院今天召開記者會,說明明年度總預算的編列方向“不會改變”...  ',
    '立法院預計下週開始審查各部會的預算案內容。',
  ].join('\r\n');

  it('probe_submit_snapshot: equivalent bodies → identical paragraph IDs; only hashes stored', async () => {
    const a = await mcp('tools/call', {
      name: 'probe_submit_snapshot',
      arguments: { url: 'https://www.cna.com.tw/news/x?utm_source=fb#top', run_label: 'run1', body_text: BODY_A, title: '標題「一」', byline: '王小明' },
    });
    const b = await mcp('tools/call', {
      name: 'probe_submit_snapshot',
      arguments: { url: 'https://www.cna.com.tw/news/x', run_label: 'run2', body_text: BODY_B, title: '標題“一”', byline: '王 小明' },
    });
    expect(a.result.isError).toBeFalsy();
    expect(a.result.structuredContent.paragraph_count).toBe(2);
    expect(b.result.structuredContent.paragraph_ids).toEqual(a.result.structuredContent.paragraph_ids);
    expect(a.result.structuredContent.rows_written).toBeGreaterThanOrEqual(1);
    expect(a.result.structuredContent.written).toBe(true);
    // The reported instrumentation cost (metrics INSERT + UPDATE, measured) is what was stored.
    const instr = a.result.structuredContent.instrumentation_rows_written;
    expect(instr).toBeGreaterThanOrEqual(2);
    const mrow = await env.DB.prepare(
      "SELECT metrics_rows_written FROM metrics WHERE tool = 'probe_submit_snapshot' ORDER BY id LIMIT 1",
    ).first<{ metrics_rows_written: number }>();
    expect(mrow!.metrics_rows_written).toBe(instr);

    const rows = await env.DB.prepare('SELECT * FROM snapshots ORDER BY created_at').all();
    expect(rows.results).toHaveLength(2);
    for (const row of rows.results) {
      expect(row.url).toBe('https://www.cna.com.tw/news/x');
      expect(row.domain).toBe('cna.com.tw');
      expect(row.account_id).toBe(424242);
      expect(String(row.title_key)).toMatch(/^[0-9a-f]{64}$/);
      const all = JSON.stringify(row);
      expect(all).not.toContain('行政院');
      expect(all).not.toContain('王小明');
    }
    expect(rows.results[0]!.title_key).toBe(rows.results[1]!.title_key);
    expect(rows.results[0]!.byline_key).toBe(rows.results[1]!.byline_key);
  });

  it('probe_submit_snapshot rejects bodies over 30000 code points and >200 paragraphs', async () => {
    const tooLong = await mcp('tools/call', {
      name: 'probe_submit_snapshot',
      arguments: { url: 'https://example.com/a', run_label: 'run1', body_text: '𠀋'.repeat(30_001) },
    });
    expect(tooLong.result?.isError ?? Boolean(tooLong.error)).toBe(true);
    const okLong = await mcp('tools/call', {
      name: 'probe_submit_snapshot',
      arguments: { url: 'https://example.com/a', run_label: 'run1', body_text: '𠀋'.repeat(30_000) },
    });
    expect(okLong.result.isError).toBeFalsy(); // 60000 UTF-16 units, but 30000 code points
    const many = Array.from({ length: 201 }, (_, i) => `第${i}段：這是一個足夠長的段落，用來測試段落數量的上限。`).join('\n');
    const tooMany = await mcp('tools/call', {
      name: 'probe_submit_snapshot',
      arguments: { url: 'https://example.com/b', run_label: 'run1', body_text: many },
    });
    expect(tooMany.result.isError).toBe(true);
    expect(tooMany.result.structuredContent.error).toBe('TOO_MANY_PARAGRAPHS');
  });

  it('probe_report_unavailable validates the reason enum and stores a row', async () => {
    const ok = await mcp('tools/call', {
      name: 'probe_report_unavailable',
      arguments: { url: 'https://paywalled.example/p', run_label: 'run1', reason: 'paywall' },
    });
    expect(ok.result.structuredContent.recorded).toBe(true);
    const bad = await mcp('tools/call', {
      name: 'probe_report_unavailable',
      arguments: { url: 'https://paywalled.example/p', run_label: 'run1', reason: 'because' },
    });
    expect(bad.result?.isError ?? Boolean(bad.error)).toBe(true);
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM unavailable').first<{ n: number }>();
    expect(n!.n).toBe(1);
  });

  it('GET /probe/report aggregates the D1 rows', async () => {
    const res = await worker('/probe/report', { headers: { Authorization: 'Bearer test-report-token' } });
    expect(res.status).toBe(200);
    const r: any = await res.json();
    expect(r.ip.mcp).toMatchObject({ observations: 2, anthropic: 1 });
    expect(r.ip.oauth).toMatchObject({ observations: 1, anthropic: 0 });
    const cna = r.urls.find((u: any) => u.url === 'https://www.cna.com.tw/news/x');
    expect(cna).toMatchObject({ submissions: 2, accounts: 1, titleAgreement: 1 });
    expect(cna.withinAccountJaccard.median).toBe(1);
    const tools = Object.fromEntries(r.metrics.map((m: any) => [m.tool, m]));
    expect(tools.probe_submit_snapshot.calls).toBe(3);
    expect(tools.probe_submit_snapshot.maxRowsWritten).toBeLessThanOrEqual(5);
    expect(tools.probe_submit_snapshot.maxRowsWrittenInclInstrumentation).toBeGreaterThan(tools.probe_submit_snapshot.maxRowsWritten);
    expect(tools.callback.calls).toBe(1);
    expect(r.summary.d1RowsWrittenTotalInclInstrumentation).toBeGreaterThan(0);
    console.log('integration metrics', JSON.stringify(r.metrics));
    const dom = Object.fromEntries(r.domains.map((d: any) => [d.domain, d]));
    expect(dom['paywalled.example']).toMatchObject({ snapshots: 0, unavailable: 1, successRate: 0 });
  });
});

describe('input limits (code points, UTF-16 pre-cap)', () => {
  it('title is limited in code points, body has a UTF-16 pre-cap', async () => {
    const okTitle = await mcpAs(accessToken, 'tools/call', {
      name: 'probe_submit_snapshot',
      arguments: { url: 'https://example.com/t', run_label: 'lim', body_text: '行政院今天召開記者會說明明年度總預算的編列方向。', title: '𠀋'.repeat(500) },
    });
    expect(okTitle.result.isError).toBeFalsy(); // 1000 UTF-16 units, 500 code points
    const longTitle = await mcpAs(accessToken, 'tools/call', {
      name: 'probe_submit_snapshot',
      arguments: { url: 'https://example.com/t', run_label: 'lim', body_text: 'x'.repeat(30), title: 'a'.repeat(501) },
    });
    expect(longTitle.result?.isError ?? Boolean(longTitle.error)).toBe(true);
    const huge = await mcpAs(accessToken, 'tools/call', {
      name: 'probe_submit_snapshot',
      arguments: { url: 'https://example.com/t', run_label: 'lim', body_text: 'a'.repeat(1_000_000) },
    });
    expect(huge.result?.isError ?? Boolean(huge.error)).toBe(true);
  });
  it('server_rejected is an accepted unavailable reason', async () => {
    const r = await mcpAs(accessToken, 'tools/call', {
      name: 'probe_report_unavailable',
      arguments: { url: 'https://example.com/b', run_label: 'run1', reason: 'server_rejected' },
    });
    expect(r.result.structuredContent).toMatchObject({ recorded: true, reason: 'server_rejected' });
  });
});

describe('access rules (spec §9.1) and OAuth error handling', () => {
  it('an account not on PROBE_ALLOWED_GITHUB_IDS is refused at /callback with access_denied and nothing is written', async () => {
    const { callback, accessToken: t } = await signIn('gh-code-outsider');
    expect(callback.status).toBe(302);
    const back = new URL(callback.headers.get('Location')!);
    expect(back.origin + back.pathname).toBe(CLIENT_REDIRECT);
    expect(back.searchParams.get('error')).toBe('access_denied');
    expect(back.searchParams.get('state')).toBe('st-gh-code-outsider');
    expect(t).toBeNull();
    for (const table of ['accounts WHERE github_id', 'ip_observations WHERE account_id', 'metrics WHERE account_id']) {
      const n = await env.DB.prepare(`SELECT COUNT(*) AS n FROM ${table} = 999`).first<{ n: number }>();
      expect(n!.n).toBe(0);
    }
  });

  it('a GitHub token-endpoint failure returns server_error to the MCP client, not a bare 500', async () => {
    const { callback } = await signIn('gh-code-down');
    expect(callback.status).toBe(302);
    const back = new URL(callback.headers.get('Location')!);
    expect(back.origin + back.pathname).toBe(CLIENT_REDIRECT);
    expect(back.searchParams.get('error')).toBe('server_error');
    expect(back.searchParams.get('state')).toBe('st-gh-code-down');
  });

  it('an account younger than 30 days can sign in and call tools, but nothing is written to D1', async () => {
    const { callback, accessToken: t } = await signIn('gh-code-new');
    expect(callback.status).toBe(302);
    expect(t).toBeTruthy();
    const who = await mcpAs(t!, 'tools/call', { name: 'probe_whoami', arguments: {} });
    expect(who.result.structuredContent).toMatchObject({ githubId: 515151, written: false, not_written_reason: 'ACCOUNT_TOO_NEW' });
    expect(who.result.structuredContent.accountAgeDays).toBeLessThan(30);
    const snap = await mcpAs(t!, 'tools/call', {
      name: 'probe_submit_snapshot',
      arguments: { url: 'https://www.cna.com.tw/news/x', run_label: 'run1', body_text: '行政院今天召開記者會說明明年度總預算的編列方向。' },
    });
    expect(snap.result.isError).toBeFalsy();
    expect(snap.result.structuredContent).toMatchObject({ written: false, not_written_reason: 'ACCOUNT_TOO_NEW', paragraph_count: 1 });
    const un = await mcpAs(t!, 'tools/call', {
      name: 'probe_report_unavailable',
      arguments: { url: 'https://paywalled.example/p', run_label: 'run1', reason: 'paywall' },
    });
    expect(un.result.structuredContent).toMatchObject({ recorded: false, written: false, reason: 'paywall', not_written_reason: 'ACCOUNT_TOO_NEW' });
    for (const [table, col] of [
      ['accounts', 'github_id'],
      ['ip_observations', 'account_id'],
      ['metrics', 'account_id'],
      ['snapshots', 'account_id'],
      ['unavailable', 'account_id'],
    ]) {
      const n = await env.DB.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE ${col} = 515151`).first<{ n: number }>();
      expect(n!.n).toBe(0);
    }
  });

  it('enforces 30 write calls per account per UTC day', async () => {
    const { accessToken: t } = await signIn('gh-code-limit');
    expect(t).toBeTruthy();
    const now = new Date().toISOString();
    const yesterday = new Date(Date.now() - DAY).toISOString();
    // 28 earlier writes today (plus 5 yesterday, which do not count), and one /callback row (not a tool).
    const fill = [
      ...Array.from({ length: 28 }, () => ['probe_submit_snapshot', now]),
      ...Array.from({ length: 5 }, () => ['probe_submit_snapshot', yesterday]),
    ].map(([tool, at]) =>
      env.DB.prepare('INSERT INTO metrics (tool, account_id, rows_read, rows_written, at) VALUES (?, 616161, 0, 0, ?)').bind(tool, at),
    );
    await env.DB.batch(fill);
    const call = () =>
      mcpAs(t!, 'tools/call', {
        name: 'probe_report_unavailable',
        arguments: { url: 'https://limit.example/a', run_label: 'run1', reason: 'fetch_error' },
      });
    expect((await call()).result.structuredContent.recorded).toBe(true); // 29th
    expect((await call()).result.structuredContent.recorded).toBe(true); // 30th
    const third = await call();
    expect(third.result.isError).toBe(true);
    expect(third.result.structuredContent).toMatchObject({ error: 'DAILY_WRITE_LIMIT', used: 30 });
    const who = await mcpAs(t!, 'tools/call', { name: 'probe_whoami', arguments: {} });
    expect(who.result.structuredContent.error).toBe('DAILY_WRITE_LIMIT');
    const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM unavailable WHERE account_id = 616161").first<{ n: number }>();
    expect(n!.n).toBe(2);
  });

  it('IPv4-mapped CF-Connecting-IP is recognised as Anthropic egress', async () => {
    const r = await mcpAs(accessToken, 'tools/call', { name: 'probe_whoami', arguments: {} }, '::ffff:160.79.104.10');
    expect(r.result.structuredContent).toMatchObject({ mcpRequestIpIsAnthropic: true, ipFamily: 'ipv4' });
  });

  it('a missing or short IP_HMAC_KEY fails closed at /callback before the state is consumed', async () => {
    const { default: mainModule } = await import('../../src/index');
    const ctx = createExecutionContext();
    const res = await mainModule.fetch(
      new Request(`${ORIGIN}/callback?code=x&state=y`, { redirect: 'manual' }),
      { ...env, IP_HMAC_KEY: 'too-short' } as any,
      ctx,
    );
    await waitOnExecutionContext(ctx);
    expect(res.status).toBe(500);
    expect(await res.text()).toMatch(/IP_HMAC_KEY/);
  });
});

describe('retention (spec §11)', () => {
  it('purges ip_observations older than 90 days and keeps newer rows', async () => {
    const old = new Date(Date.now() - 91 * DAY).toISOString();
    await env.DB.prepare(
      "INSERT INTO ip_observations (account_id, source, is_anthropic, prefix_hmac, at) VALUES (424242, 'oauth', 0, '', ?)",
    )
      .bind(old)
      .run();
    const before = await env.DB.prepare('SELECT COUNT(*) AS n FROM ip_observations').first<{ n: number }>();
    expect(await purgeOldIpObservations(env.DB)).toBe(1);
    const after = await env.DB.prepare('SELECT COUNT(*) AS n FROM ip_observations').first<{ n: number }>();
    expect(after!.n).toBe(before!.n - 1);
    expect(after!.n).toBeGreaterThan(0);
  });
});
