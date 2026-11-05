/**
 * P0 report (spec §4.2 items 2–5). Pure function over raw Cloudflare D1 rows so it can be unit
 * tested without a database.
 */

export interface SnapshotRow {
  id: string;
  account_id: number;
  url: string;
  domain: string;
  run_label: string;
  title_key: string;
  byline_key: string;
  published_at: string;
  paragraph_ids_json: string;
  paragraph_count: number;
  body_codepoints: number;
  created_at: string;
}
export interface UnavailableRow {
  id: string;
  account_id: number;
  url: string;
  domain: string;
  run_label: string;
  reason: string;
  created_at: string;
}
export interface IpObservationRow {
  account_id: number;
  source: 'mcp' | 'oauth';
  is_anthropic: number | boolean;
  prefix_hmac: string;
  at: string;
}
export interface MetricRow {
  tool: string;
  account_id?: number;
  rows_read: number;
  rows_written: number;
  /** Instrumentation cost of writing this metrics row (INSERT + UPDATE); 0 if unknown. */
  metrics_rows_written?: number;
  at: string;
}

/** Unavailable reason that means "fetched, but the server refused the body": not a fetch failure. */
export const NON_FETCH_FAILURE_REASONS: ReadonlySet<string> = new Set(['server_rejected']);
export interface ReportInput {
  snapshots: SnapshotRow[];
  unavailable: UnavailableRow[];
  ipObservations: IpObservationRow[];
  metrics: MetricRow[];
}

/** Pass/fail thresholds (RUNBOOK results table). */
export const THRESHOLDS = {
  crossAccountJaccardMedian: 0.8,
  attestedShare: 0.6,
  urlsPassingRequired: 7, // of 10 URLs
  urlsPlanned: 10,
  domainSuccessRateForUsable: 0.5,
  usableDomainShare: 0.7,
  maxRowsWrittenPerSnapshot: 5,
} as const;

export interface Stats {
  n: number;
  min: number | null;
  median: number | null;
  mean: number | null;
  max: number | null;
}

export function stats(xs: number[]): Stats {
  if (xs.length === 0) return { n: 0, min: null, median: null, mean: null, max: null };
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  const median = s.length % 2 === 1 ? s[mid]! : (s[mid - 1]! + s[mid]!) / 2;
  const mean = s.reduce((a, b) => a + b, 0) / s.length;
  return { n: s.length, min: s[0]!, median, mean, max: s[s.length - 1]! };
}

/** Jaccard index |A∩B| / |A∪B| of two sets; two empty sets count as 1. */
export function jaccard(a: ReadonlySet<string>, b: ReadonlySet<string>): number {
  if (a.size === 0 && b.size === 0) return 1;
  let inter = 0;
  for (const x of a) if (b.has(x)) inter++;
  return inter / (a.size + b.size - inter);
}

function parseIds(json: string): string[] {
  try {
    const v: unknown = JSON.parse(json);
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

function round(x: number | null, digits = 4): number | null {
  if (x === null) return null;
  const f = 10 ** digits;
  return Math.round(x * f) / f;
}

function roundStats(s: Stats): Stats {
  return { n: s.n, min: round(s.min), median: round(s.median), mean: round(s.mean), max: round(s.max) };
}

/** Share of rows whose key equals the most common non-empty key. null when no row has a key. */
function agreement(keys: string[]): { share: number | null; distinct: number } {
  const nonEmpty = keys.filter((k) => k !== '');
  if (nonEmpty.length === 0) return { share: null, distinct: 0 };
  const counts = new Map<string, number>();
  for (const k of nonEmpty) counts.set(k, (counts.get(k) ?? 0) + 1);
  const top = Math.max(...counts.values());
  return { share: round(top / keys.length), distinct: counts.size };
}

export interface UrlReport {
  url: string;
  domain: string;
  submissions: number;
  accounts: number;
  runs: string[];
  /** Jaccard of paragraph-ID sets over every pair of submissions from DIFFERENT accounts. */
  crossAccountJaccard: Stats;
  /** Jaccard over every pair of submissions from the SAME account (across runs). */
  withinAccountJaccard: Stats;
  /**
   * Median-length submission (spec §6.1-6): median by body_codepoints; with an even number of
   * submissions the upper middle one (ties broken by paragraph_count, created_at, id).
   */
  medianSubmissionId: string | null;
  medianSubmissionCodepoints: number | null;
  /** Distinct paragraph IDs of the median-length submission (the §6.1-6 denominator). */
  medianParagraphCount: number;
  /** Distinct paragraph IDs OF THE MEDIAN SUBMISSION that appear in submissions of ≥2 distinct accounts. */
  attestedParagraphs: number;
  /** attestedParagraphs / medianParagraphCount (≤ 1 by construction). */
  attestedShare: number | null;
  titleAgreement: number | null;
  titleDistinct: number;
  bylineAgreement: number | null;
  bylineDistinct: number;
  unavailable: number;
  /** Evaluated only with ≥2 accounts: Jaccard median ≥ 0.8 AND attestedShare ≥ 0.6. */
  pass: boolean | null;
}

export interface DomainReport {
  domain: string;
  snapshots: number;
  unavailable: number;
  successRate: number | null;
  reasons: Record<string, number>;
  usable: boolean;
}

export interface MetricsReport {
  tool: string;
  calls: number;
  /** The call's own statements (what production would also run). */
  avgRowsWritten: number | null;
  maxRowsWritten: number | null;
  avgRowsRead: number | null;
  maxRowsRead: number | null;
  /** Own statements + the probe's metrics-row instrumentation: what the call cost on the P0 deployment. */
  avgRowsWrittenInclInstrumentation: number | null;
  maxRowsWrittenInclInstrumentation: number | null;
  totalRowsWrittenInclInstrumentation: number;
}

export interface IpSideReport {
  observations: number;
  anthropic: number;
  share: number | null;
  distinctPrefixes: number;
}

export interface Report {
  generatedAt: string;
  thresholds: typeof THRESHOLDS;
  urls: UrlReport[];
  domains: DomainReport[];
  ip: { mcp: IpSideReport; oauth: IpSideReport };
  metrics: MetricsReport[];
  summary: {
    urlsWithSnapshots: number;
    urlsEvaluated: number;
    urlsPassing: number;
    item2ParagraphMatchPass: boolean;
    domainCount: number;
    usableDomains: number;
    overallFetchSuccessRate: number | null;
    item3FetchPass: boolean;
    item4McpIpIsAnthropic: boolean | null;
    item5RowsWrittenPerSnapshotMax: number | null;
    item5RowsWrittenPass: boolean | null;
    /** Same, including the metrics-row instrumentation the probe adds (not part of production). */
    item5RowsWrittenPerSnapshotMaxInclInstrumentation: number | null;
    /** All D1 rows written by measured calls (tools + /callback), including instrumentation. KV is not measured here. */
    d1RowsWrittenTotalInclInstrumentation: number;
  };
}

function urlReport(url: string, snaps: SnapshotRow[], unavailableCount: number, domain: string): UrlReport {
  const sets = snaps.map((s) => ({ account: s.account_id, ids: new Set(parseIds(s.paragraph_ids_json)) }));
  const cross: number[] = [];
  const within: number[] = [];
  for (let i = 0; i < sets.length; i++) {
    for (let j = i + 1; j < sets.length; j++) {
      const v = jaccard(sets[i]!.ids, sets[j]!.ids);
      (sets[i]!.account === sets[j]!.account ? within : cross).push(v);
    }
  }
  // Which accounts attest each paragraph ID.
  const attesters = new Map<string, Set<number>>();
  for (const s of sets) {
    for (const id of s.ids) {
      let a = attesters.get(id);
      if (!a) attesters.set(id, (a = new Set()));
      a.add(s.account);
    }
  }
  // Median-length submission (spec §6.1-6 "中位長度提交"): median by body length in code points.
  // Even count → upper middle (the longer of the two middle submissions).
  const order = snaps
    .map((s, i) => ({ s, ids: sets[i]!.ids }))
    .sort(
      (a, b) =>
        a.s.body_codepoints - b.s.body_codepoints ||
        a.s.paragraph_count - b.s.paragraph_count ||
        (a.s.created_at < b.s.created_at ? -1 : a.s.created_at > b.s.created_at ? 1 : 0) ||
        (a.s.id < b.s.id ? -1 : a.s.id > b.s.id ? 1 : 0),
    );
  const median = order.length ? order[Math.floor(order.length / 2)]! : null;
  // Numerator and denominator both come from the median submission, so paragraphs that only
  // longer submissions contain cannot inflate the share.
  let attested = 0;
  if (median) for (const id of median.ids) if ((attesters.get(id)?.size ?? 0) >= 2) attested++;
  const medianParagraphCount = median ? median.ids.size : 0;
  const attestedShare = medianParagraphCount > 0 ? round(attested / medianParagraphCount) : null;

  const accounts = new Set(snaps.map((s) => s.account_id)).size;
  const crossStats = roundStats(stats(cross));
  const title = agreement(snaps.map((s) => s.title_key));
  const byline = agreement(snaps.map((s) => s.byline_key));
  const pass =
    accounts >= 2 && crossStats.median !== null && attestedShare !== null
      ? crossStats.median >= THRESHOLDS.crossAccountJaccardMedian && attestedShare >= THRESHOLDS.attestedShare
      : null;
  return {
    url,
    domain,
    submissions: snaps.length,
    accounts,
    runs: [...new Set(snaps.map((s) => s.run_label))].sort(),
    crossAccountJaccard: crossStats,
    withinAccountJaccard: roundStats(stats(within)),
    medianSubmissionId: median?.s.id ?? null,
    medianSubmissionCodepoints: median?.s.body_codepoints ?? null,
    medianParagraphCount,
    attestedParagraphs: attested,
    attestedShare,
    titleAgreement: title.share,
    titleDistinct: title.distinct,
    bylineAgreement: byline.share,
    bylineDistinct: byline.distinct,
    unavailable: unavailableCount,
    pass,
  };
}

function ipSide(rows: IpObservationRow[]): IpSideReport {
  const anthropic = rows.filter((r) => r.is_anthropic === true || r.is_anthropic === 1).length;
  return {
    observations: rows.length,
    anthropic,
    share: rows.length ? round(anthropic / rows.length) : null,
    distinctPrefixes: new Set(rows.map((r) => r.prefix_hmac).filter((p) => p !== '')).size,
  };
}

export function buildReport(input: ReportInput, now: Date = new Date()): Report {
  // ---- per URL
  const byUrl = new Map<string, SnapshotRow[]>();
  const domainOfUrl = new Map<string, string>();
  for (const s of input.snapshots) {
    let l = byUrl.get(s.url);
    if (!l) byUrl.set(s.url, (l = []));
    l.push(s);
    domainOfUrl.set(s.url, s.domain);
  }
  const unavailByUrl = new Map<string, number>();
  for (const u of input.unavailable) {
    unavailByUrl.set(u.url, (unavailByUrl.get(u.url) ?? 0) + 1);
    if (!domainOfUrl.has(u.url)) domainOfUrl.set(u.url, u.domain);
  }
  const allUrls = [...new Set([...byUrl.keys(), ...unavailByUrl.keys()])].sort();
  const urls = allUrls.map((url) =>
    urlReport(url, byUrl.get(url) ?? [], unavailByUrl.get(url) ?? 0, domainOfUrl.get(url) ?? ''),
  );

  // ---- per domain
  // `unavailable` counts fetch failures only; server_rejected rows are listed in `reasons` but
  // excluded from the success rate (the page was fetched; the server refused the body).
  const dom = new Map<string, { snapshots: number; unavailable: number; reasons: Record<string, number> }>();
  const d = (name: string) => {
    let x = dom.get(name);
    if (!x) dom.set(name, (x = { snapshots: 0, unavailable: 0, reasons: {} }));
    return x;
  };
  for (const s of input.snapshots) d(s.domain).snapshots++;
  for (const u of input.unavailable) {
    const x = d(u.domain);
    if (!NON_FETCH_FAILURE_REASONS.has(u.reason)) x.unavailable++;
    x.reasons[u.reason] = (x.reasons[u.reason] ?? 0) + 1;
  }
  const domains: DomainReport[] = [...dom.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([domain, x]) => {
      const total = x.snapshots + x.unavailable;
      const successRate = total ? round(x.snapshots / total) : null;
      return {
        domain,
        snapshots: x.snapshots,
        unavailable: x.unavailable,
        successRate,
        reasons: x.reasons,
        usable: successRate !== null && successRate >= THRESHOLDS.domainSuccessRateForUsable,
      };
    });

  // ---- metrics
  const byTool = new Map<string, MetricRow[]>();
  for (const m of input.metrics) {
    let l = byTool.get(m.tool);
    if (!l) byTool.set(m.tool, (l = []));
    l.push(m);
  }
  const metrics: MetricsReport[] = [...byTool.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([tool, rows]) => {
      const w = stats(rows.map((r) => r.rows_written));
      const r = stats(rows.map((x) => x.rows_read));
      const totals = rows.map((x) => x.rows_written + (x.metrics_rows_written ?? 0));
      const t = stats(totals);
      return {
        tool,
        calls: rows.length,
        avgRowsWritten: round(w.mean),
        maxRowsWritten: w.max,
        avgRowsRead: round(r.mean),
        maxRowsRead: r.max,
        avgRowsWrittenInclInstrumentation: round(t.mean),
        maxRowsWrittenInclInstrumentation: t.max,
        totalRowsWrittenInclInstrumentation: totals.reduce((a, b) => a + b, 0),
      };
    });

  // ---- IP
  const mcp = ipSide(input.ipObservations.filter((o) => o.source === 'mcp'));
  const oauth = ipSide(input.ipObservations.filter((o) => o.source === 'oauth'));

  // ---- summary
  const evaluated = urls.filter((u) => u.pass !== null);
  const passing = evaluated.filter((u) => u.pass === true).length;
  const totalSnap = input.snapshots.length;
  const totalUnavail = input.unavailable.filter((u) => !NON_FETCH_FAILURE_REASONS.has(u.reason)).length;
  const usableDomains = domains.filter((x) => x.usable).length;
  const snapMetric = metrics.find((m) => m.tool === 'probe_submit_snapshot');
  return {
    generatedAt: now.toISOString(),
    thresholds: THRESHOLDS,
    urls,
    domains,
    ip: { mcp, oauth },
    metrics,
    summary: {
      urlsWithSnapshots: byUrl.size,
      urlsEvaluated: evaluated.length,
      urlsPassing: passing,
      item2ParagraphMatchPass: passing >= THRESHOLDS.urlsPassingRequired,
      domainCount: domains.length,
      usableDomains,
      overallFetchSuccessRate: totalSnap + totalUnavail ? round(totalSnap / (totalSnap + totalUnavail)) : null,
      item3FetchPass: domains.length > 0 && usableDomains / domains.length >= THRESHOLDS.usableDomainShare,
      item4McpIpIsAnthropic: mcp.share === null ? null : mcp.share >= 0.5,
      item5RowsWrittenPerSnapshotMax: snapMetric?.maxRowsWritten ?? null,
      item5RowsWrittenPass: snapMetric?.maxRowsWritten == null ? null : snapMetric.maxRowsWritten <= THRESHOLDS.maxRowsWrittenPerSnapshot,
      item5RowsWrittenPerSnapshotMaxInclInstrumentation: snapMetric?.maxRowsWrittenInclInstrumentation ?? null,
      d1RowsWrittenTotalInclInstrumentation: metrics.reduce((a, m) => a + m.totalRowsWrittenInclInstrumentation, 0),
    },
  };
}

/** Load every probe table from Cloudflare D1 and build the report. P0 data is small (≪ 10k rows). */
export async function loadReport(db: D1Database): Promise<Report> {
  const [s, u, ip, m] = await db.batch([
    db.prepare('SELECT * FROM snapshots ORDER BY created_at'),
    db.prepare('SELECT * FROM unavailable ORDER BY created_at'),
    db.prepare('SELECT account_id, source, is_anthropic, prefix_hmac, at FROM ip_observations ORDER BY at'),
    db.prepare('SELECT tool, account_id, rows_read, rows_written, metrics_rows_written, at FROM metrics ORDER BY at'),
  ]);
  return buildReport({
    snapshots: (s!.results ?? []) as unknown as SnapshotRow[],
    unavailable: (u!.results ?? []) as unknown as UnavailableRow[],
    ipObservations: (ip!.results ?? []) as unknown as IpObservationRow[],
    metrics: (m!.results ?? []) as unknown as MetricRow[],
  });
}
