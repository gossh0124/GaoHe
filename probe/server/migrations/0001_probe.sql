-- GaoHe P0 feasibility probe schema (Cloudflare D1).
-- Stores hashes and counts only: no article body text is ever written (spec §5.1).
-- No secondary indexes on purpose, so rows_written per tool call reflects the bare table write;
-- production tables will add index writes on top of this.
-- Retention (spec §11): ip_observations rows older than 90 days are deleted by the Worker's daily
-- cron (src/retention.ts); RUNBOOK §13 deletes everything after the probe.

CREATE TABLE accounts (
  github_id          INTEGER PRIMARY KEY,          -- GitHub numeric user id (spec §9.1)
  github_created_at  TEXT    NOT NULL,
  first_seen_at      TEXT    NOT NULL,
  last_seen_at       TEXT    NOT NULL
);

CREATE TABLE ip_observations (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id    INTEGER NOT NULL,                  -- accounts.github_id
  source        TEXT    NOT NULL CHECK (source IN ('mcp', 'oauth')),
  is_anthropic  INTEGER NOT NULL CHECK (is_anthropic IN (0, 1)),
  prefix_hmac   TEXT    NOT NULL,                  -- HMAC-SHA256(IP_HMAC_KEY, /24 or /48 prefix), '' if unknown
  at            TEXT    NOT NULL
);

CREATE TABLE snapshots (
  id                  TEXT    PRIMARY KEY,         -- random UUID
  account_id          INTEGER NOT NULL,
  url                 TEXT    NOT NULL,            -- canonicalized (fragment and tracking params removed)
  domain              TEXT    NOT NULL,
  run_label           TEXT    NOT NULL,
  title_key           TEXT    NOT NULL,            -- sha256(matchKey(title)) hex, '' if no title
  byline_key          TEXT    NOT NULL,            -- sha256(matchKey(byline)) hex, '' if no byline
  published_at        TEXT    NOT NULL,            -- as reported by the client, '' if unknown
  paragraph_ids_json  TEXT    NOT NULL,            -- JSON array of sha256(matchKey(paragraph)) in order
  paragraph_count     INTEGER NOT NULL,
  body_codepoints     INTEGER NOT NULL,
  created_at          TEXT    NOT NULL
);

CREATE TABLE unavailable (
  id          TEXT    PRIMARY KEY,
  account_id  INTEGER NOT NULL,
  url         TEXT    NOT NULL,
  domain      TEXT    NOT NULL,
  run_label   TEXT    NOT NULL,
  -- server_rejected: fetched, but probe_submit_snapshot refused the body; excluded from fetch success rate
  reason      TEXT    NOT NULL CHECK (reason IN ('paywall', 'login', 'blocked', 'boilerplate_only', 'fetch_error', 'other', 'server_rejected')),
  created_at  TEXT    NOT NULL
);

CREATE TABLE metrics (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  tool                  TEXT    NOT NULL,          -- probe_* tool name, or 'callback' for /callback
  account_id            INTEGER NOT NULL,          -- accounts.github_id (daily write limit, spec §9.1)
  rows_read             INTEGER NOT NULL,          -- sum of D1 meta.rows_read of the call's own statements
  rows_written          INTEGER NOT NULL,          -- sum of D1 meta.rows_written of the call's own statements
  metrics_rows_written  INTEGER NOT NULL DEFAULT 0, -- instrumentation cost: this row's INSERT + UPDATE
  at                    TEXT    NOT NULL
);
