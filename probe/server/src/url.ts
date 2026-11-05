const TRACKING_PARAMS = /^(utm_[a-z_]+|fbclid|gclid|yclid|mc_cid|mc_eid|igshid|ref|ref_src|from)$/i;

/**
 * Canonical article URL used to group submissions: http(s) only, lowercase host, no fragment,
 * no default port, common tracking parameters removed, remaining params sorted.
 * Returns null for anything that is not an absolute http(s) URL.
 */
export function canonicalizeUrl(raw: string): { url: string; domain: string } | null {
  let u: URL;
  try {
    u = new URL(raw.trim());
  } catch {
    return null;
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
  if (u.username || u.password) return null;
  u.hash = '';
  const kept = [...u.searchParams.entries()].filter(([k]) => !TRACKING_PARAMS.test(k));
  kept.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  u.search = '';
  for (const [k, v] of kept) u.searchParams.append(k, v);
  const domain = u.hostname.toLowerCase().replace(/^www\./, '');
  return { url: u.toString(), domain };
}
