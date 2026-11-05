import { expandIPv6, toIPv4 } from './anthropicEgress';

export { expandIPv6, toIPv4 };

/**
 * Network prefix used as a clustering signal (spec §9.4): IPv4 /24, IPv6 /48.
 * Returns e.g. "203.0.113.0/24" or "2001:db8:1::/48"; null for anything unparseable.
 * IPv4-mapped IPv6 (::ffff:a.b.c.d) is treated as the IPv4 address, so it gets its own /24 instead
 * of every mapped address collapsing into one ::ffff /48.
 */
export function ipPrefix(ip: string): string | null {
  const t = ip.trim();
  const v4 = toIPv4(t);
  if (v4 !== null) {
    return `${v4 >>> 24}.${(v4 >>> 16) & 0xff}.${(v4 >>> 8) & 0xff}.0/24`;
  }
  const v6 = expandIPv6(t);
  if (v6) return `${v6.slice(0, 3).map((h) => h.toString(16)).join(':')}::/48`;
  return null;
}

/** Hex HMAC-SHA256(key, message). */
export async function hmacHex(key: string, message: string): Promise<string> {
  const k = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(key),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const sig = await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** Minimum IP_HMAC_KEY length in UTF-8 bytes (RUNBOOK: `openssl rand -hex 32` gives 64). */
export const MIN_IP_HMAC_KEY_BYTES = 32;

export class MisconfiguredError extends Error {}

/** Throws MisconfiguredError unless IP_HMAC_KEY is set to at least MIN_IP_HMAC_KEY_BYTES bytes. */
export function assertIpHmacKey(key: string | undefined | null): asserts key is string {
  if (typeof key !== 'string' || new TextEncoder().encode(key).length < MIN_IP_HMAC_KEY_BYTES) {
    throw new MisconfiguredError(`IP_HMAC_KEY secret must be set to at least ${MIN_IP_HMAC_KEY_BYTES} bytes`);
  }
}

/**
 * HMAC of the /24 or /48 prefix of an IP; "" when the IP is missing or unparseable.
 * Fails closed (MisconfiguredError) when the key is missing or shorter than 32 bytes.
 */
export async function ipPrefixHmac(key: string, ip: string | null | undefined): Promise<string> {
  assertIpHmacKey(key);
  if (!ip) return '';
  const prefix = ipPrefix(ip);
  return prefix ? hmacHex(key, prefix) : '';
}

/** Constant-time string comparison (for bearer tokens). */
export function timingSafeEqualStr(a: string, b: string): boolean {
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  let diff = ea.length ^ eb.length;
  const n = Math.max(ea.length, eb.length);
  for (let i = 0; i < n; i++) diff |= (ea[i] ?? 0) ^ (eb[i] ?? 0);
  return diff === 0;
}
