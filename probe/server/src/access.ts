/**
 * Who may write (spec §9.1, P0 subset):
 *   - PROBE_ALLOWED_GITHUB_IDS: P0-only allowlist of GitHub numeric user ids (comma or whitespace
 *     separated). Empty or unset = nobody (fail closed). Checked at /callback before any write and
 *     again on every tool call, so removing an id takes effect without revoking grants.
 *   - GitHub accounts younger than MIN_ACCOUNT_AGE_DAYS may use the tools but nothing is written.
 *   - At most DAILY_WRITE_LIMIT write-type tool calls per account per UTC day.
 */
import { accountAgeDays } from './github';

export const MIN_ACCOUNT_AGE_DAYS = 30;
export const DAILY_WRITE_LIMIT = 30;
/** Tools that write to Cloudflare D1 and therefore count toward the daily limit. */
export const WRITE_TOOLS = ['probe_whoami', 'probe_submit_snapshot', 'probe_report_unavailable'] as const;

export function parseAllowlist(raw: string | undefined | null): Set<number> {
  const out = new Set<number>();
  for (const tok of (raw ?? '').split(/[\s,]+/)) {
    if (/^[1-9]\d{0,15}$/.test(tok)) out.add(Number(tok));
  }
  return out;
}

export function isAllowedGitHubId(raw: string | undefined | null, githubId: number): boolean {
  return parseAllowlist(raw).has(githubId);
}

export function isTooNew(githubCreatedAt: string, now: Date = new Date()): boolean {
  return accountAgeDays(githubCreatedAt, now) < MIN_ACCOUNT_AGE_DAYS;
}

/** Start of the current UTC day as an ISO string (comparable with the stored `at` values). */
export function utcDayStart(now: Date = new Date()): string {
  return `${now.toISOString().slice(0, 10)}T00:00:00.000Z`;
}
