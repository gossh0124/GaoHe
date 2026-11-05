import { describe, expect, it } from 'vitest';
import vectors from '../../../../schemas/normalize-vectors.json';
import { codePointLength, matchKey, paragraphId, sha256Hex, splitParagraphs } from '../../src/normalize';

describe('matchKey vectors', () => {
  for (const v of vectors.matchKey) {
    it(v.name, () => {
      expect(matchKey(v.input)).toBe(v.expectedMatchKey);
    });
  }
  it('is idempotent on every vector', () => {
    for (const v of vectors.matchKey) expect(matchKey(matchKey(v.input))).toBe(matchKey(v.input));
  });
});

describe('splitParagraphs vectors', () => {
  for (const v of vectors.splitParagraphs) {
    it(v.name, () => {
      expect(splitParagraphs(v.body)).toEqual(v.expectedParagraphs);
    });
  }
});

describe('paragraphId', () => {
  for (const v of vectors.paragraphIdEquivalence) {
    it(v.name, async () => {
      const ids = await Promise.all(v.variants.map(paragraphId));
      expect(new Set(ids).size).toBe(1);
    });
  }
  it('is lowercase hex sha256 of the match key', async () => {
    expect(await sha256Hex('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect(await paragraphId(' a b c ')).toBe(await sha256Hex('abc'));
  });
  it('differs for different text', async () => {
    expect(await paragraphId('甲')).not.toBe(await paragraphId('乙'));
  });
});

describe('codePointLength', () => {
  it('counts astral characters as one', () => {
    expect('𠀋'.length).toBe(2);
    expect(codePointLength('𠀋')).toBe(1);
    expect(codePointLength('a😀b')).toBe(3);
    expect(codePointLength('')).toBe(0);
  });
});
