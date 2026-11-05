import { describe, expect, it } from 'vitest';
import {
  accountAgeDays,
  buildGitHubAuthorizeUrl,
  exchangeGitHubCode,
  fetchGitHubUser,
  newCodeVerifier,
  s256Challenge,
} from '../../src/github';

describe('buildGitHubAuthorizeUrl', () => {
  it('builds the authorize URL with empty scope, state and PKCE', () => {
    const u = new URL(
      buildGitHubAuthorizeUrl({
        clientId: 'Iv1.abc',
        redirectUri: 'https://gaohe-probe.example.workers.dev/callback',
        state: 's t&x',
        codeChallenge: 'chal',
      }),
    );
    expect(u.origin + u.pathname).toBe('https://github.com/login/oauth/authorize');
    expect(u.searchParams.get('client_id')).toBe('Iv1.abc');
    expect(u.searchParams.get('redirect_uri')).toBe('https://gaohe-probe.example.workers.dev/callback');
    expect(u.searchParams.get('state')).toBe('s t&x');
    expect(u.searchParams.get('scope')).toBe('');
    expect(u.searchParams.get('code_challenge')).toBe('chal');
    expect(u.searchParams.get('code_challenge_method')).toBe('S256');
  });
  it('omits PKCE params without a challenge', () => {
    const u = new URL(buildGitHubAuthorizeUrl({ clientId: 'c', redirectUri: 'https://x/cb', state: 's' }));
    expect(u.searchParams.has('code_challenge')).toBe(false);
  });
});

describe('PKCE', () => {
  it('RFC 7636 appendix B vector', async () => {
    expect(await s256Challenge('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk')).toBe(
      'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
    );
  });
  it('verifier is 43 url-safe chars', () => {
    expect(newCodeVerifier()).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });
});

describe('exchange + user', () => {
  it('posts the code and returns the token; reads numeric id and created_at', async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const fake = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (url.includes('access_token')) return Response.json({ access_token: 'gho_x', token_type: 'bearer', scope: '' });
      return Response.json({ id: 12345, login: 'someone', created_at: '2015-01-02T03:04:05Z', extra: 1 });
    };
    const token = await exchangeGitHubCode(
      { clientId: 'c', clientSecret: 's', code: 'k', redirectUri: 'https://x/cb', codeVerifier: 'v' },
      fake,
    );
    expect(token).toBe('gho_x');
    const body = new URLSearchParams(String(calls[0]!.init!.body));
    expect(body.get('code')).toBe('k');
    expect(body.get('code_verifier')).toBe('v');
    const user = await fetchGitHubUser(token, fake);
    expect(user).toEqual({ id: 12345, created_at: '2015-01-02T03:04:05Z' });
    expect(new Headers(calls[1]!.init!.headers).get('Authorization')).toBe('Bearer gho_x');
  });
  it('throws on a GitHub error payload', async () => {
    const fake = async () => Response.json({ error: 'bad_verification_code' });
    await expect(
      exchangeGitHubCode({ clientId: 'c', clientSecret: 's', code: 'k', redirectUri: 'https://x/cb' }, fake),
    ).rejects.toThrow(/bad_verification_code/);
  });
  it('accountAgeDays', () => {
    expect(accountAgeDays('2026-10-01T00:00:00Z', new Date('2026-11-05T12:00:00Z'))).toBe(35);
    expect(accountAgeDays('garbage')).toBe(0);
  });
});
