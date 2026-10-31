-- HTSG archive schema, generation 7 (终版设计 §5, §13.2, §13.6; 修复轮14).
-- Only for NEW databases: a database created by the frame-based generation (schema_version 1-4) or by generation 5 or 6
-- is recognised and refused, never upgraded in place.  Generation numbers continue the legacy numbering so one number
-- never means two layouts.
-- commit_group, sample and state_event are partitioned by bucket - the unit the rolling deletion takes away whole.  Their
-- partitions are made by the maintenance thread (ArchiveStore.prepare_bucket); the bucket list says which buckets this
-- database has and what each may take.  Every other table is kept for good.

CREATE TABLE IF NOT EXISTS htsg_meta (key text PRIMARY KEY, value text NOT NULL);

-- Full configuration snapshot per config id: every run and fault points at one.
CREATE TABLE IF NOT EXISTS config_version (
    id text PRIMARY KEY, payload jsonb NOT NULL, saved_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS run (
    run_id text PRIMARY KEY, config_id text NOT NULL REFERENCES config_version(id),
    started_wall double precision NOT NULL, ended_wall double precision, end_reason text,
    last_evidence_wall double precision
);

-- One row per archive segment.  A connection generation may hold several segments: an archive
-- gap, a clock jump or a same-generation recovery cuts the valid interval without a reconnect.
-- A segment's start and end may be committed into different databases after a route switch, so each
-- database can hold a partial row; queries merge the rows of one (run, source, seg).
CREATE TABLE IF NOT EXISTS segment (
    run_id text NOT NULL, source_id text NOT NULL, seg integer NOT NULL, conn_gen integer,
    start_wall double precision, start_reason text,
    end_wall double precision, end_reason text,
    PRIMARY KEY (run_id, source_id, seg)
);
CREATE INDEX IF NOT EXISTS segment_start ON segment(start_wall);

CREATE TABLE IF NOT EXISTS archive_gap (
    id text PRIMARY KEY, run_id text NOT NULL, source_id text NOT NULL,
    start_wall double precision NOT NULL, end_wall double precision, reason text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS archive_gap_start ON archive_gap(start_wall);

-- Latest confirmed watermark per segment: history is certain only up to covered_wall.
CREATE TABLE IF NOT EXISTS watermark (
    run_id text NOT NULL, source_id text NOT NULL, seg integer NOT NULL,
    covered_wall double precision NOT NULL, group_order bigint NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, source_id, seg)
);

-- READY: prepared, empty.  CURRENT: the newest bucket this database has received a group of.  SEALED: an older one; it
-- still takes the groups given to it before it was sealed.  DELETING / DELETED: being / been taken away, it takes nothing.
CREATE TABLE IF NOT EXISTS bucket (
    no bigint PRIMARY KEY CHECK(no > 0),
    state text NOT NULL DEFAULT 'READY' CHECK(state IN ('READY','CURRENT','SEALED','DELETING','DELETED')),
    prepared_at timestamptz NOT NULL DEFAULT now(), changed_at timestamptz NOT NULL DEFAULT now(),
    baseline_wall double precision,  -- the earliest retention baseline stored in this bucket here: its retention boundary
    used boolean NOT NULL DEFAULT false,  -- it received a group here; a prepared bucket never used does not count (§13.6)
    first_wall double precision, last_wall double precision,  -- the time its groups covered, taken when it is marked DELETING
    -- the earliest and latest time its rows here are filed under (effective time) and its groups were built at, widened in
    -- the transaction that writes them: a query names only the buckets this can reach; NULL (not known) is always read
    data_lo double precision, data_hi double precision
);

-- The bucket is part of every key: "this group was written already" is asked of the group's own bucket.  The record
-- id leads, so a lookup by id still uses the index.
CREATE TABLE IF NOT EXISTS commit_group (
    bucket bigint NOT NULL, group_id text NOT NULL, order_no bigint NOT NULL, run_id text NOT NULL,
    source_id text NOT NULL, kind text NOT NULL, records integer NOT NULL, built_wall double precision NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, bucket)
) PARTITION BY LIST (bucket);
CREATE INDEX IF NOT EXISTS commit_group_order ON commit_group(run_id, order_no);
CREATE INDEX IF NOT EXISTS commit_group_wall ON commit_group(built_wall);  -- the time a bucket covers, per partition

-- Generation 7 (修复轮14): every input value and derived result is ONE narrow row of `sample`, nothing else per row.
-- The channel and the run are small numbers each database gives on its own (`channel`, `run_number`); `state` is a code
-- of `state_code` with bit 0x4000 marking a value beyond its physical limits; `ua_status` is the OPC UA status code as a
-- signed 32-bit integer (0 for every other row); `item` orders the rows of one batch that share a time.  `t` is the time
-- the value counts from: its receipt, or the time it was written again (a restatement or a retention baseline).
-- Columns are laid out widest first, so a row without NULLs takes no alignment padding.
CREATE TABLE IF NOT EXISTS channel (
    no smallint PRIMARY KEY, point_id text NOT NULL, source_id text NOT NULL, UNIQUE (point_id, source_id)
);
CREATE TABLE IF NOT EXISTS run_number (no integer PRIMARY KEY, run_id text NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS state_code (code smallint PRIMARY KEY, key text NOT NULL, meaning text NOT NULL);

CREATE TABLE IF NOT EXISTS sample (
    t double precision NOT NULL, value double precision,
    bucket integer NOT NULL, run integer NOT NULL, seg integer NOT NULL, ua_status integer NOT NULL,
    channel smallint NOT NULL, state smallint NOT NULL, item smallint NOT NULL
) PARTITION BY LIST (bucket);
CREATE INDEX IF NOT EXISTS sample_channel_time ON sample(channel, t);

-- For a technician reading the database by hand: the same rows in words.
CREATE OR REPLACE VIEW sample_view AS
SELECT to_timestamp(s.t) AS time, c.point_id, c.source_id, s.value, coalesce(k.meaning, '未知') AS state,
       CASE WHEN s.ua_status <> 0 THEN '0x' || upper(to_hex(s.ua_status)) END AS ua_status,
       (s.state & 16384) <> 0 AS beyond_limits, r.run_id, s.seg, s.bucket
FROM sample s
LEFT JOIN channel c ON c.no = s.channel
LEFT JOIN run_number r ON r.no = s.run
LEFT JOIN state_code k ON k.code = (s.state & 16383);

-- Only the retention baselines are kept here (修复轮14): the replay reads which sources each one restated.  Segment
-- starts and ends, gaps, watermarks and run ends go to their own tables.
CREATE TABLE IF NOT EXISTS state_event (
    bucket bigint NOT NULL, id text NOT NULL, group_id text NOT NULL, run_id text NOT NULL, source_id text NOT NULL,
    seg integer NOT NULL, kind text NOT NULL, at_wall double precision NOT NULL,
    at_mono double precision NOT NULL, detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, bucket)
) PARTITION BY LIST (bucket);
CREATE INDEX IF NOT EXISTS state_event_time ON state_event(source_id, at_wall);
CREATE INDEX IF NOT EXISTS state_event_group ON state_event(group_id);

CREATE TABLE IF NOT EXISTS fault_record (
    id text PRIMARY KEY, group_id text NOT NULL, run_id text NOT NULL, source_id text NOT NULL,
    seg integer NOT NULL, batch_seq bigint NOT NULL, point_id text NOT NULL, direction text NOT NULL,
    confirmed_wall double precision NOT NULL, confirmed_mono double precision NOT NULL,
    basis_kind text NOT NULL, basis_id text NOT NULL, basis_wall double precision NOT NULL,
    basis_value double precision, config_id text NOT NULL REFERENCES config_version(id),
    rule jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS fault_record_time ON fault_record(confirmed_wall DESC, id DESC);
CREATE INDEX IF NOT EXISTS fault_record_point ON fault_record(point_id, confirmed_wall DESC, id DESC);
CREATE INDEX IF NOT EXISTS fault_record_group ON fault_record(group_id);

-- Bounded summary of fault records refused by a full buffer (终版 §12): a gap, not a found fault.
CREATE TABLE IF NOT EXISTS fault_loss (
    id text PRIMARY KEY, group_id text NOT NULL, run_id text NOT NULL, source_id text NOT NULL,
    lost_count integer NOT NULL, first_wall double precision NOT NULL, last_wall double precision NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS routing_state (
    id integer PRIMARY KEY CHECK(id=1), mode text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS write_probe (id integer PRIMARY KEY CHECK(id=1), touched_at timestamptz NOT NULL);

CREATE TABLE IF NOT EXISTS account (
    username text PRIMARY KEY, role text NOT NULL CHECK(role IN ('admin','viewer')),
    salt text NOT NULL, digest text NOT NULL, active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS management_log (
    id text PRIMARY KEY, time double precision NOT NULL, actor text NOT NULL,
    action text NOT NULL, target text NOT NULL, result text NOT NULL
);
CREATE INDEX IF NOT EXISTS management_log_time ON management_log(time DESC, id DESC);
