/**
 * Anthropic egress detection for spec §4.2-4 / §9.4.
 *
 * 160.79.104.0/21 is taken from Anthropic's published IP address list ("IP addresses" page in the
 * Claude / API docs: outbound requests from Anthropic, e.g. remote MCP connector calls, come from
 * this range). It MUST be re-checked against the published list before P0 measurements are run
 * and whenever the list changes. IPv6 egress ranges, if Anthropic publishes any, are NOT covered
 * here: this function returns false for every IPv6 input except IPv4-mapped addresses
 * (::ffff:a.b.c.d / ::ffff:XXXX:XXXX), which are unmapped to IPv4 first.
 */
export const ANTHROPIC_EGRESS_V4_CIDR = '160.79.104.0/21';

/** Parse a dotted-quad IPv4 string strictly (no leading zeros, no extra parts) to a uint32. */
export function parseIPv4(ip: string): number | null {
  const parts = ip.split('.');
  if (parts.length !== 4) return null;
  let n = 0;
  for (const p of parts) {
    if (!/^(0|[1-9]\d{0,2})$/.test(p)) return null;
    const v = Number(p);
    if (v > 255) return null;
    n = n * 256 + v;
  }
  return n >>> 0;
}

/** Expand an IPv6 address to 8 hextets, or null if it is not valid IPv6. Zone IDs are rejected. */
export function expandIPv6(ip: string): number[] | null {
  if (!ip.includes(':') || ip.includes('%')) return null;
  let s = ip.toLowerCase();
  // Embedded IPv4 tail (e.g. ::ffff:1.2.3.4)
  const v4 = s.match(/^(.*:)(\d+\.\d+\.\d+\.\d+)$/);
  if (v4) {
    const n = parseIPv4(v4[2]!);
    if (n === null) return null;
    s = `${v4[1]}${((n >>> 16) & 0xffff).toString(16)}:${(n & 0xffff).toString(16)}`;
  }
  const halves = s.split('::');
  if (halves.length > 2) return null;
  const parse = (h: string) => (h === '' ? [] : h.split(':'));
  const head = parse(halves[0]!);
  const tail = halves.length === 2 ? parse(halves[1]!) : [];
  const missing = 8 - head.length - tail.length;
  if (halves.length === 1 ? missing !== 0 : missing < 1) return null;
  const all = [...head, ...Array<string>(halves.length === 2 ? missing : 0).fill('0'), ...tail];
  const out: number[] = [];
  for (const h of all) {
    if (!/^[0-9a-f]{1,4}$/.test(h)) return null;
    out.push(parseInt(h, 16));
  }
  return out.length === 8 ? out : null;
}

/**
 * IPv4 address as uint32 for a dotted quad OR an IPv4-mapped IPv6 address (::ffff:0:0/96, in either
 * dotted or hex tail form). null for anything else, including other IPv6 addresses.
 */
export function toIPv4(ip: string): number | null {
  const t = ip.trim();
  const v4 = parseIPv4(t);
  if (v4 !== null) return v4;
  const v6 = expandIPv6(t);
  if (!v6 || v6.slice(0, 5).some((h) => h !== 0) || v6[5] !== 0xffff) return null;
  return ((v6[6]! << 16) | v6[7]!) >>> 0;
}

function cidrContains(cidr: string, ip: number): boolean {
  const [base, bitsStr] = cidr.split('/');
  const baseN = parseIPv4(base!);
  const bits = Number(bitsStr);
  if (baseN === null || !Number.isInteger(bits) || bits < 0 || bits > 32) return false;
  const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
  return ((ip & mask) >>> 0) === ((baseN & mask) >>> 0);
}

/** True iff `ip` (IPv4, or IPv4-mapped IPv6) is inside Anthropic's published egress range. */
export function isAnthropicEgress(ip: string): boolean {
  const n = toIPv4(ip);
  if (n === null) return false;
  return cidrContains(ANTHROPIC_EGRESS_V4_CIDR, n);
}
