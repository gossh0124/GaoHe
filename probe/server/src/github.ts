/**
 * Upstream GitHub OAuth helpers (spec §9.1). GitHub is used ONLY to learn the numeric user id and
 * account creation time. No scopes are requested (empty scope = public profile read only) and the
 * GitHub access token is never stored: it is used once to call GET /user and then dropped.
 */
import { z } from 'zod';

export const GITHUB_AUTHORIZE_URL = 'https://github.com/login/oauth/authorize';
export const GITHUB_TOKEN_URL = 'https://github.com/login/oauth/access_token';
export const GITHUB_USER_URL = 'https://api.github.com/user';
const USER_AGENT = 'gaohe-probe (+https://github.com/)';

export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export interface AuthorizeUrlParams {
  clientId: string;
  redirectUri: string;
  state: string;
  /** S256 PKCE challenge for the upstream leg (GitHub supports PKCE for OAuth Apps). */
  codeChallenge?: string;
}

export function buildGitHubAuthorizeUrl(p: AuthorizeUrlParams): string {
  const u = new URL(GITHUB_AUTHORIZE_URL);
  u.searchParams.set('client_id', p.clientId);
  u.searchParams.set('redirect_uri', p.redirectUri);
  u.searchParams.set('state', p.state);
  u.searchParams.set('scope', ''); // no scopes: public profile only
  u.searchParams.set('allow_signup', 'false');
  if (p.codeChallenge) {
    u.searchParams.set('code_challenge', p.codeChallenge);
    u.searchParams.set('code_challenge_method', 'S256');
  }
  return u.toString();
}

function base64url(bytes: Uint8Array): string {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** 43-char PKCE code verifier from 32 random bytes. */
export function newCodeVerifier(): string {
  return base64url(crypto.getRandomValues(new Uint8Array(32)));
}

export async function s256Challenge(verifier: string): Promise<string> {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(d));
}

const TokenResponse = z.object({
  access_token: z.string().min(1).optional(),
  error: z.string().optional(),
  error_description: z.string().optional(),
});

export interface ExchangeParams {
  clientId: string;
  clientSecret: string;
  code: string;
  redirectUri: string;
  codeVerifier?: string;
}

/** Exchange an authorization code for a GitHub access token (returned to the caller, never stored). */
export async function exchangeGitHubCode(p: ExchangeParams, fetchImpl: FetchLike = fetch): Promise<string> {
  const body = new URLSearchParams({
    client_id: p.clientId,
    client_secret: p.clientSecret,
    code: p.code,
    redirect_uri: p.redirectUri,
  });
  if (p.codeVerifier) body.set('code_verifier', p.codeVerifier);
  const res = await fetchImpl(GITHUB_TOKEN_URL, {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/x-www-form-urlencoded',
      'User-Agent': USER_AGENT,
    },
    body,
  });
  if (!res.ok) throw new Error(`github token endpoint HTTP ${res.status}`);
  const parsed = TokenResponse.parse(await res.json());
  if (!parsed.access_token) throw new Error(`github token error: ${parsed.error ?? 'unknown'}`);
  return parsed.access_token;
}

const GitHubUser = z.object({
  id: z.number().int().positive(),
  created_at: z.string().min(1),
});
export type GitHubUser = z.infer<typeof GitHubUser>;

export async function fetchGitHubUser(token: string, fetchImpl: FetchLike = fetch): Promise<GitHubUser> {
  const res = await fetchImpl(GITHUB_USER_URL, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': USER_AGENT,
    },
  });
  if (!res.ok) throw new Error(`github /user HTTP ${res.status}`);
  const u = GitHubUser.parse(await res.json());
  return { id: u.id, created_at: u.created_at };
}

/** Whole days between GitHub account creation and `now`. */
export function accountAgeDays(createdAt: string, now: Date = new Date()): number {
  const t = Date.parse(createdAt);
  if (Number.isNaN(t)) return 0;
  return Math.floor((now.getTime() - t) / 86_400_000);
}
