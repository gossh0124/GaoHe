import { describe, expect, it } from 'vitest';
import { buildReport, jaccard, stats, type SnapshotRow, type UnavailableRow } from '../../src/report';
import { DAILY_WRITE_LIMIT, isAllowedGitHubId, isTooNew, parseAllowlist, utcDayStart } from '../../src/access';

const ACC1 = 1001;
const ACC2 = 2002;
const P = Array.from({ length: 10 }, (_, i) => `p${i + 1}`);
const Q = Array.from({ length: 10 }, (_, i) => `q${i + 1}`);
const R = ['q1', 'q2', 'q3', ...Array.from({ length: 7 }, (_, i) => `r${i + 1}`)];

let n = 0;
function snap(
  account: number,
  url: string,
  domain: string,
  run: string,
  ids: string[],
  title = 't',
  byline = 'b',
  codepoints = ids.length * 50,
): SnapshotRow {
  n++;
  return {
    id: `s${n}`,
    account_id: account,
    url,
    domain,
    run_label: run,
    title_key: title,
    byline_key: byline,
    published_at: '',
    paragraph_ids_json: JSON.stringify(ids),
    paragraph_count: ids.length,
    body_codepoints: codepoints,
    created_at: `2026-11-05T00:00:${String(n).padStart(2, '0')}Z`,
  };
}
function unavail(account: number, url: string, domain: string, reason: string): UnavailableRow {
  n++;
  return { id: `u${n}`, account_id: account, url, domain, run_label: 'run1', reason, created_at: '2026-11-05T00:00:00Z' };
}

const A = 'https://a.example/stable';
const B = 'https://a.example/unstable';
const C = 'https://c.example/one';
const D = 'https://d.example/blocked';

const snapshots: SnapshotRow[] = [
  // URL A: 2 accounts x 3 runs, noise on account 2 (one dropped paragraph, one extra "ad" paragraph, one title variant)
  snap(ACC1, A, 'a.example', 'run1', P),
  snap(ACC1, A, 'a.example', 'run2', P),
  snap(ACC1, A, 'a.example', 'run3', P),
  snap(ACC2, A, 'a.example', 'run1', P),
  snap(ACC2, A, 'a.example', 'run2', P.slice(0, 9)),
  snap(ACC2, A, 'a.example', 'run3', [...P, 'x1'], 't2'),
  // URL B: 2 accounts x 3 runs, different segmentation across accounts (3 shared paragraphs)
  ...['run1', 'run2', 'run3'].map((r) => snap(ACC1, B, 'a.example', r, Q, 't', '')),
  ...['run1', 'run2', 'run3'].map((r) => snap(ACC2, B, 'a.example', r, R, 't', '')),
  // URL C: one account only
  snap(ACC1, C, 'c.example', 'run1', ['c1', 'c2']),
];
const unavailable: UnavailableRow[] = [
  unavail(ACC2, C, 'c.example', 'paywall'),
  unavail(ACC1, D, 'd.example', 'blocked'),
  unavail(ACC2, D, 'd.example', 'fetch_error'),
  // Fetched but refused by the server: listed, but not a fetch failure.
  unavail(ACC1, C, 'c.example', 'server_rejected'),
  unavail(ACC2, C, 'c.example', 'server_rejected'),
];

const report = buildReport(
  {
    snapshots,
    unavailable,
    ipObservations: [
      { account_id: ACC1, source: 'mcp', is_anthropic: 1, prefix_hmac: 'h1', at: '' },
      { account_id: ACC2, source: 'mcp', is_anthropic: 1, prefix_hmac: 'h1', at: '' },
      { account_id: ACC2, source: 'mcp', is_anthropic: 0, prefix_hmac: 'h2', at: '' },
      { account_id: ACC1, source: 'oauth', is_anthropic: 0, prefix_hmac: 'o1', at: '' },
      { account_id: ACC2, source: 'oauth', is_anthropic: 0, prefix_hmac: 'o2', at: '' },
    ],
    metrics: [
      { tool: 'probe_submit_snapshot', rows_read: 0, rows_written: 1, metrics_rows_written: 3, at: '' },
      { tool: 'probe_submit_snapshot', rows_read: 0, rows_written: 1, metrics_rows_written: 3, at: '' },
      { tool: 'probe_submit_snapshot', rows_read: 2, rows_written: 2, metrics_rows_written: 3, at: '' },
      { tool: 'probe_whoami', rows_read: 0, rows_written: 1, at: '' }, // older row without the column
      { tool: 'callback', rows_read: 1, rows_written: 3, metrics_rows_written: 3, at: '' },
    ],
  },
  new Date('2026-11-05T12:00:00Z'),
);
const byUrl = Object.fromEntries(report.urls.map((u) => [u.url, u]));

describe('helpers', () => {
  it('jaccard', () => {
    expect(jaccard(new Set(['a', 'b']), new Set(['b', 'c']))).toBeCloseTo(1 / 3);
    expect(jaccard(new Set(), new Set())).toBe(1);
  });
  it('stats median even/odd', () => {
    expect(stats([3, 1, 2]).median).toBe(2);
    expect(stats([4, 1, 2, 3]).median).toBe(2.5);
    expect(stats([]).median).toBeNull();
  });
});

describe('buildReport per URL', () => {
  it('stable URL: cross-account median 10/11, within-account median, attested 100%', () => {
    const a = byUrl[A]!;
    expect(a.submissions).toBe(6);
    expect(a.accounts).toBe(2);
    expect(a.runs).toEqual(['run1', 'run2', 'run3']);
    expect(a.crossAccountJaccard.n).toBe(9);
    expect(a.crossAccountJaccard.median).toBeCloseTo(10 / 11, 4);
    expect(a.crossAccountJaccard.min).toBeCloseTo(0.9, 4);
    expect(a.crossAccountJaccard.max).toBe(1);
    expect(a.withinAccountJaccard.n).toBe(6);
    expect(a.withinAccountJaccard.median).toBeCloseTo((10 / 11 + 1) / 2, 4);
    expect(a.withinAccountJaccard.min).toBeCloseTo(9 / 11, 4);
    // body_codepoints 450, 500×4, 550 → upper middle (index 3) is a 10-paragraph submission
    expect(a.medianSubmissionCodepoints).toBe(500);
    expect(a.medianParagraphCount).toBe(10);
    expect(a.attestedParagraphs).toBe(10);
    expect(a.attestedShare).toBe(1);
    expect(a.titleAgreement).toBeCloseTo(5 / 6, 4);
    expect(a.titleDistinct).toBe(2);
    expect(a.bylineAgreement).toBe(1);
    expect(a.pass).toBe(true);
  });
  it('unstable URL fails both criteria', () => {
    const b = byUrl[B]!;
    expect(b.crossAccountJaccard.median).toBeCloseTo(3 / 17, 4);
    expect(b.withinAccountJaccard.median).toBe(1);
    expect(b.attestedParagraphs).toBe(3);
    expect(b.attestedShare).toBeCloseTo(0.3, 4);
    expect(b.bylineAgreement).toBeNull();
    expect(b.pass).toBe(false);
  });
  it('single-account URL is not evaluated', () => {
    const c = byUrl[C]!;
    expect(c.accounts).toBe(1);
    expect(c.crossAccountJaccard.n).toBe(0);
    expect(c.unavailable).toBe(3); // per-URL count lists every unavailable row
    expect(c.pass).toBeNull();
  });
  it('attested share counts only paragraphs OF the median submission (boilerplate in long ones cannot inflate it)', () => {
    const U = 'https://e.example/inflate';
    const core = ['k1', 'k2', 'k3', 'k4', 'k5'];
    const extra = Array.from({ length: 6 }, (_, i) => `boiler${i}`);
    // Short submissions share only k1,k2 across accounts; long ones share 6 boilerplate ids.
    const rows = [
      snap(ACC1, U, 'e.example', 'run1', core, 't', 'b', 500),
      snap(ACC2, U, 'e.example', 'run1', ['k1', 'k2', 'z3', 'z4', 'z5'], 't', 'b', 510),
      snap(ACC1, U, 'e.example', 'run2', [...core, ...extra], 't', 'b', 2000),
      snap(ACC2, U, 'e.example', 'run2', ['y1', ...extra], 't', 'b', 2100),
      snap(ACC1, U, 'e.example', 'run3', core, 't', 'b', 505),
    ];
    const u = buildReport({ snapshots: rows, unavailable: [], ipObservations: [], metrics: [] }).urls[0]!;
    // Sorted by code points: 500, 505, 510, 2000, 2100 → median = 510 (ACC2 run1).
    expect(u.medianSubmissionCodepoints).toBe(510);
    expect(u.medianParagraphCount).toBe(5);
    // Of the median's ids only k1,k2 are attested by both accounts. The old global count
    // (k1,k2 + 6 boilerplate = 8, capped to 5/5) reported 1.0.
    expect(u.attestedParagraphs).toBe(2);
    expect(u.attestedShare).toBeCloseTo(0.4, 4);
  });
  it('even number of submissions → upper middle by body_codepoints, not lower middle by paragraph count', () => {
    const U = 'https://e.example/even';
    const rows = [
      snap(ACC1, U, 'e.example', 'run1', ['a1', 'a2'], 't', 'b', 100),
      snap(ACC2, U, 'e.example', 'run1', ['a1', 'a2', 'a3'], 't', 'b', 300),
      snap(ACC1, U, 'e.example', 'run2', ['a1', 'a2', 'a3', 'a4'], 't', 'b', 200), // more paragraphs, shorter body
      snap(ACC2, U, 'e.example', 'run2', ['a1', 'a2', 'a3', 'a4', 'a5'], 't', 'b', 400),
    ];
    const u = buildReport({ snapshots: rows, unavailable: [], ipObservations: [], metrics: [] }).urls[0]!;
    expect(u.medianSubmissionCodepoints).toBe(300);
    expect(u.medianParagraphCount).toBe(3);
    expect(u.attestedParagraphs).toBe(3);
    expect(u.attestedShare).toBe(1);
  });
  it('unavailable-only URL is listed with zero submissions', () => {
    const d = byUrl[D]!;
    expect(d.submissions).toBe(0);
    expect(d.unavailable).toBe(2);
    expect(d.domain).toBe('d.example');
    expect(d.pass).toBeNull();
  });
});

describe('buildReport per domain, IP, metrics, summary', () => {
  it('domain success rates', () => {
    const dom = Object.fromEntries(report.domains.map((d) => [d.domain, d]));
    expect(dom['a.example']).toMatchObject({ snapshots: 12, unavailable: 0, successRate: 1, usable: true });
    // 2 server_rejected rows are listed in reasons but not counted as fetch failures
    expect(dom['c.example']).toMatchObject({
      snapshots: 1,
      unavailable: 1,
      successRate: 0.5,
      usable: true,
      reasons: { paywall: 1, server_rejected: 2 },
    });
    expect(dom['d.example']).toMatchObject({
      snapshots: 0,
      unavailable: 2,
      successRate: 0,
      usable: false,
      reasons: { blocked: 1, fetch_error: 1 },
    });
  });
  it('IP summary', () => {
    expect(report.ip.mcp).toEqual({ observations: 3, anthropic: 2, share: 0.6667, distinctPrefixes: 2 });
    expect(report.ip.oauth).toEqual({ observations: 2, anthropic: 0, share: 0, distinctPrefixes: 2 });
  });
  it('metrics', () => {
    const snapM = report.metrics.find((m) => m.tool === 'probe_submit_snapshot')!;
    expect(snapM).toEqual({
      tool: 'probe_submit_snapshot',
      calls: 3,
      avgRowsWritten: 1.3333,
      maxRowsWritten: 2,
      avgRowsRead: 0.6667,
      maxRowsRead: 2,
      avgRowsWrittenInclInstrumentation: 4.3333,
      maxRowsWrittenInclInstrumentation: 5,
      totalRowsWrittenInclInstrumentation: 13,
    });
    expect(report.metrics.find((m) => m.tool === 'probe_whoami')!.totalRowsWrittenInclInstrumentation).toBe(1);
    expect(report.metrics.find((m) => m.tool === 'callback')).toMatchObject({ calls: 1, maxRowsWrittenInclInstrumentation: 6 });
  });
  it('summary', () => {
    expect(report.generatedAt).toBe('2026-11-05T12:00:00.000Z');
    expect(report.summary).toEqual({
      urlsWithSnapshots: 3,
      urlsEvaluated: 2,
      urlsPassing: 1,
      item2ParagraphMatchPass: false,
      domainCount: 3,
      usableDomains: 2,
      overallFetchSuccessRate: 0.8125, // 13 snapshots / (13 + 3 unavailable); server_rejected excluded
      item3FetchPass: false,
      item4McpIpIsAnthropic: true,
      item5RowsWrittenPerSnapshotMax: 2,
      item5RowsWrittenPass: true,
      item5RowsWrittenPerSnapshotMaxInclInstrumentation: 5,
      d1RowsWrittenTotalInclInstrumentation: 20, // 13 snapshot + 1 whoami + 6 callback
    });
  });
});

describe('access rules (spec §9.1)', () => {
  it('allowlist parsing fails closed', () => {
    expect([...parseAllowlist('1001, 2002\n3003 ,x, -5, 0')]).toEqual([1001, 2002, 3003]);
    expect(parseAllowlist('').size).toBe(0);
    expect(parseAllowlist(undefined).size).toBe(0);
    expect(isAllowedGitHubId(undefined, 1001)).toBe(false);
    expect(isAllowedGitHubId('1001,2002', 2002)).toBe(true);
    expect(isAllowedGitHubId('10011', 1001)).toBe(false);
  });
  it('30-day rule', () => {
    const now = new Date('2026-11-05T12:00:00Z');
    expect(isTooNew('2026-10-07T12:00:01Z', now)).toBe(true); // 29 days
    expect(isTooNew('2026-10-06T12:00:00Z', now)).toBe(false); // 30 days
    expect(isTooNew('not a date', now)).toBe(true);
  });
  it('daily window is the UTC day', () => {
    expect(utcDayStart(new Date('2026-11-05T23:59:59Z'))).toBe('2026-11-05T00:00:00.000Z');
    expect(DAILY_WRITE_LIMIT).toBe(30);
  });
});
