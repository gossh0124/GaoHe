/**
 * Per-call Cloudflare D1 cost instrumentation (spec §4.2-5).
 *
 * Each metrics row stores:
 *   rows_read / rows_written   meta of the call's own statements (what production will also run)
 *   metrics_rows_written       the probe's own instrumentation cost for that call: the metrics
 *                              INSERT's meta.rows_written plus the single-row UPDATE that stores it
 *                              (the UPDATE's own meta is returned so tests can check it is 1).
 * rows_written + metrics_rows_written is what the call really cost on the P0 deployment, so the
 * report's totals reconcile with the Cloudflare dashboard.
 */
import type { Env } from './env';

export interface MetricCost {
  rows_read: number;
  rows_written: number;
  /** INSERT + UPDATE of the metrics row itself. */
  metrics_rows_written: number;
}

export async function recordMetric(
  env: Env,
  tool: string,
  accountId: number,
  results: D1Result[],
): Promise<MetricCost> {
  const rows_read = results.reduce((a, r) => a + (r.meta?.rows_read ?? 0), 0);
  const rows_written = results.reduce((a, r) => a + (r.meta?.rows_written ?? 0), 0);
  const ins = await env.DB.prepare(
    'INSERT INTO metrics (tool, account_id, rows_read, rows_written, metrics_rows_written, at) VALUES (?, ?, ?, ?, 0, ?)',
  )
    .bind(tool, accountId, rows_read, rows_written, new Date().toISOString())
    .run();
  const insertCost = ins.meta?.rows_written ?? 0;
  // The UPDATE touches exactly one existing row of a table with no secondary index: 1 row written.
  const stored = insertCost + 1;
  const upd = await env.DB.prepare('UPDATE metrics SET metrics_rows_written = ? WHERE id = ?')
    .bind(stored, ins.meta.last_row_id)
    .run();
  const updateCost = upd.meta?.rows_written ?? 0;
  return { rows_read, rows_written, metrics_rows_written: insertCost + updateCost };
}
