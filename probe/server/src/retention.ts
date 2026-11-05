/** Retention (spec §11: IP signals are kept 90 days). Run daily by the Worker's cron trigger. */
export const IP_OBSERVATION_RETENTION_DAYS = 90;

/** Deletes ip_observations older than the retention window; returns the number of rows deleted. */
export async function purgeOldIpObservations(db: D1Database, now: Date = new Date()): Promise<number> {
  const cutoff = new Date(now.getTime() - IP_OBSERVATION_RETENTION_DAYS * 86_400_000).toISOString();
  const r = await db.prepare('DELETE FROM ip_observations WHERE at < ?').bind(cutoff).run();
  return r.meta?.changes ?? 0;
}
