import { describe, expect, it } from 'vitest';
import { canonicalizeUrl } from '../../src/url';

describe('canonicalizeUrl', () => {
  it('drops fragment and tracking params, sorts the rest, lowercases host, strips www for domain', () => {
    expect(canonicalizeUrl('https://WWW.Example.com.tw/news/1?utm_source=x&b=2&a=1&fbclid=z#top')).toEqual({
      url: 'https://www.example.com.tw/news/1?a=1&b=2',
      domain: 'example.com.tw',
    });
  });
  it('rejects non-http(s) and credentials', () => {
    expect(canonicalizeUrl('javascript:alert(1)')).toBeNull();
    expect(canonicalizeUrl('ftp://x/y')).toBeNull();
    expect(canonicalizeUrl('https://u:p@x/y')).toBeNull();
    expect(canonicalizeUrl('not a url')).toBeNull();
  });
});
