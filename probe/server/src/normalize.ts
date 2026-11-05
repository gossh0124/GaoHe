/**
 * Match-key normalization and paragraph splitting (spec §0 比對鍵, §6.1-3).
 * Deterministic, no AI. All lengths are Unicode code points, never UTF-16 units.
 */

/** Canonical quote character every quote variant maps to. */
export const CANONICAL_QUOTE = '"';
/** Canonical dash character every dash variant (and runs of them) maps to. */
export const CANONICAL_DASH = '-';
/** Canonical ellipsis every ellipsis variant (and runs of them) maps to. */
export const CANONICAL_ELLIPSIS = '…';

/** Paragraphs whose match key is shorter than this are merged into the next one. */
export const MIN_PARAGRAPH_KEY_CODEPOINTS = 20;

// Format characters (General_Category=Cf: ZWSP U+200B, ZWJ/ZWNJ, BOM U+FEFF, soft hyphen U+00AD,
// bidi marks, tag characters …) and variation selectors (U+FE00–FE0F, U+E0100–E01EF; these are Mn,
// not Cf, so they are listed explicitly).
const FORMAT_AND_VS = /[\p{Cf}\uFE00-\uFE0F\u{E0100}-\u{E01EF}]/gu;
// JS \s covers ASCII whitespace, NBSP, U+1680, U+2000–200A, U+2028/2029, U+202F, U+205F, U+3000, U+FEFF.
// U+3000 is listed explicitly for clarity.
const WHITESPACE = /[\s\u3000]/gu;
// After NFKC: full-width ＂＇ have already become " and '. 〝〞〟 are CJK double-prime quotes.
// « » ‹ › (guillemets) are quotation marks too. Deliberately NOT included: 〃 U+3003 (ditto mark)
// and ″ U+2033 / ′ U+2032 (primes; NFKC turns ″ into ′′): they are not quotation marks and also
// appear in measurements such as 5′11″.
const QUOTES = /[\u201C\u201D\u201E\u201F\u300C\u300D\u300E\u300F"'\u2018\u2019\u201A\u201B\u301D\u301E\u301F\u00AB\u00BB\u2039\u203A]/gu;
// — – ― ‐ ‑ ‒ − ⸺ ⸻ and ASCII '-'. A run (e.g. Chinese "——") collapses to one '-'.
// ⸺ U+2E3A / ⸻ U+2E3B (two- and three-em dash) are single characters used in place of "——".
// Deliberately NOT included: ー U+30FC (katakana prolonged sound mark, a CJK letter) and 一 U+4E00.
const DASH_RUNS = /[\u2014\u2013\u2015\u2010\u2011\u2012\u2212\u2E3A\u2E3B-]+/gu;
// NFKC turns … (U+2026) into "...", so "…", "……", "...", "......" all arrive as dot runs.
// ⋯ (U+22EF, midline ellipsis) has no decomposition and is handled directly.
// 。。。 is intentionally NOT mapped (it is ordinary punctuation repetition).
const ELLIPSIS_RUNS = /(?:\.{3,}|[\u2026\u22EF])+/gu;

/**
 * Match key (spec §6.1-3). Order:
 *   1. remove Cf and variation selectors (done first so that a ZWSP between a base letter and a
 *      combining mark cannot block NFKC composition),
 *   2. NFKC,
 *   3. remove all whitespace (NFKC can produce spaces, e.g. U+3000 → U+0020),
 *   4. map quote / dash / ellipsis variants to one canonical form each.
 * The spec lists "NFKC → remove → map"; doing the removal both before NFKC and again after is
 * equivalent for every input where removal does not interact with composition, and more stable
 * where it does.
 */
export function matchKey(s: string): string {
  let k = s.replace(FORMAT_AND_VS, '');
  k = k.normalize('NFKC');
  k = k.replace(FORMAT_AND_VS, '');
  k = k.replace(WHITESPACE, '');
  k = k.replace(QUOTES, CANONICAL_QUOTE);
  k = k.replace(DASH_RUNS, CANONICAL_DASH);
  k = k.replace(ELLIPSIS_RUNS, CANONICAL_ELLIPSIS);
  return k;
}

/** Length in Unicode code points (astral chars like 𠀋 or 😀 count as 1). */
export function codePointLength(s: string): number {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

// Line breaks: CRLF, LF, CR, VT (U+000B), FF (U+000C), NEL, LS, PS (the Unicode mandatory breaks).
const LINE_BREAKS = /\r\n|[\n\r\u000B\u000C\u2028\u2029\u0085]/u;

/**
 * Deterministic paragraph split (spec §6.1-3):
 *  - split on line breaks, trim, drop lines whose match key is empty
 *    (this also drops lines made only of zero-width characters);
 *  - a paragraph whose match key is < MIN_PARAGRAPH_KEY_CODEPOINTS code points is merged into the
 *    next one (joined with "\n"), repeatedly, until the accumulated key is long enough;
 *  - a short remainder at the very end is merged into the previous paragraph (if any).
 */
export function splitParagraphs(body: string): string[] {
  const lines = body
    .split(LINE_BREAKS)
    .map((l) => l.trim())
    .filter((l) => matchKey(l).length > 0);

  const out: string[] = [];
  let buf: string | null = null;
  for (const line of lines) {
    buf = buf === null ? line : `${buf}\n${line}`;
    if (codePointLength(matchKey(buf)) >= MIN_PARAGRAPH_KEY_CODEPOINTS) {
      out.push(buf);
      buf = null;
    }
  }
  if (buf !== null) {
    if (out.length > 0) out[out.length - 1] = `${out[out.length - 1]}\n${buf}`;
    else out.push(buf);
  }
  return out;
}

/** Hex SHA-256 of the UTF-8 bytes of a string. */
export async function sha256Hex(s: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** Paragraph ID = hex sha256(matchKey(p)) (spec §0 段落 ID). */
export async function paragraphId(p: string): Promise<string> {
  return sha256Hex(matchKey(p));
}
