import { describe, expect, it } from 'vitest';
import { isAnthropicEgress, parseIPv4 } from '../../src/anthropicEgress';
import { expandIPv6, hmacHex, ipPrefix, ipPrefixHmac, MisconfiguredError, timingSafeEqualStr } from '../../src/ip';

describe('isAnthropicEgress (160.79.104.0/21)', () => {
  it.each([
    ['160.79.104.0', true],
    ['160.79.104.1', true],
    ['160.79.107.200', true],
    ['160.79.111.255', true],
    [' 160.79.105.9 ', true],
    ['160.79.103.255', false],
    ['160.79.112.0', false],
    ['160.79.120.1', false],
    ['8.8.8.8', false],
    ['', false],
    ['160.79.104', false],
    ['160.79.104.256', false],
    ['160.79.104.01', false],
    ['2607:6bc0::10', false],
    ['::ffff:160.79.104.10', true], // IPv4-mapped IPv6 is unmapped first
    ['::FFFF:a04f:680a', true], // same address, hex tail
    ['::ffff:8.8.8.8', false],
    ['::160.79.104.10', false], // IPv4-compatible (deprecated), not mapped
    ['64:ff9b::a04f:680a', false], // NAT64, not mapped
    ['not an ip', false],
  ])('%s → %s', (ip, expected) => {
    expect(isAnthropicEgress(ip)).toBe(expected);
  });
  it('parseIPv4', () => {
    expect(parseIPv4('255.255.255.255')).toBe(0xffffffff);
    expect(parseIPv4('0.0.0.0')).toBe(0);
    expect(parseIPv4('1.2.3.4.5')).toBeNull();
  });
});

describe('ip prefixes', () => {
  it('IPv4 /24', () => {
    expect(ipPrefix('203.0.113.77')).toBe('203.0.113.0/24');
  });
  it('IPv6 /48', () => {
    expect(ipPrefix('2001:db8:abcd:12::1')).toBe('2001:db8:abcd::/48');
    expect(ipPrefix('2001:0DB8:0001:ffff:0:0:0:1')).toBe('2001:db8:1::/48');
    expect(ipPrefix('::1')).toBe('0:0:0::/48');
  });
  it('IPv4-mapped IPv6 → the IPv4 /24, not one shared ::ffff /48', () => {
    expect(ipPrefix('::ffff:1.2.3.4')).toBe('1.2.3.0/24');
    expect(ipPrefix('::ffff:9.9.9.9')).toBe('9.9.9.0/24');
    expect(ipPrefix('::ffff:0102:0304')).toBe('1.2.3.0/24');
    expect(ipPrefix('0:0:0:0:0:ffff:203.0.113.9')).toBe('203.0.113.0/24');
  });
  it('rejects garbage', () => {
    expect(ipPrefix('nope')).toBeNull();
    expect(expandIPv6('1::2::3')).toBeNull();
    expect(expandIPv6('1:2:3:4:5:6:7:8:9')).toBeNull();
    expect(expandIPv6('fe80::1%eth0')).toBeNull();
  });
  it('expands embedded IPv4', () => {
    expect(expandIPv6('::ffff:1.2.3.4')).toEqual([0, 0, 0, 0, 0, 0xffff, 0x0102, 0x0304]);
  });
  it('HMAC-SHA256 (RFC 4231 test case 2)', async () => {
    expect(await hmacHex('Jefe', 'what do ya want for nothing?')).toBe(
      '5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843',
    );
  });
  it('ipPrefixHmac: same /24 → same HMAC, different /24 → different, missing → ""', async () => {
    const k = 'k'.repeat(32);
    expect(await ipPrefixHmac(k, '198.51.100.1')).toBe(await ipPrefixHmac(k, '198.51.100.254'));
    expect(await ipPrefixHmac(k, '198.51.100.1')).not.toBe(await ipPrefixHmac(k, '198.51.101.1'));
    expect(await ipPrefixHmac(k, null)).toBe('');
    expect(await ipPrefixHmac(k, 'garbage')).toBe('');
  });
  it('ipPrefixHmac fails closed on a missing or < 32-byte IP_HMAC_KEY', async () => {
    await expect(ipPrefixHmac(undefined as unknown as string, '198.51.100.1')).rejects.toBeInstanceOf(MisconfiguredError);
    await expect(ipPrefixHmac('', '198.51.100.1')).rejects.toThrow(/IP_HMAC_KEY/);
    await expect(ipPrefixHmac('k'.repeat(31), '198.51.100.1')).rejects.toThrow(/at least 32 bytes/);
    await expect(ipPrefixHmac('k'.repeat(31), null)).rejects.toThrow(/IP_HMAC_KEY/); // even without an IP
    await expect(ipPrefixHmac('鍵'.repeat(11), '198.51.100.1')).resolves.toMatch(/^[0-9a-f]{64}$/); // 33 UTF-8 bytes
  });
  it('timingSafeEqualStr', () => {
    expect(timingSafeEqualStr('abc', 'abc')).toBe(true);
    expect(timingSafeEqualStr('abc', 'abd')).toBe(false);
    expect(timingSafeEqualStr('abc', 'abcd')).toBe(false);
    expect(timingSafeEqualStr('', '')).toBe(true);
  });
});
