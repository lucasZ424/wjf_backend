-- HTSG archive schema, generation 5 (终版设计 §5, §10, §13.2).
-- Typed records, idempotent by stable id.  Only for NEW databases: a database created by the
-- frame-based generation (schema_version 1-4) is recognised and refused, never upgraded in place.
-- Generation numbers continue the legacy numbering so one number never means two layouts.

CREATE TABLE IF NOT EXISTS htsg_meta (key text PRIMARY KEY, value text NOT NULL);

-- Full configuration snapshot per config id: every derived result and fault points at one.
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
CREATE TABLE IF NOT EXISTS segment (
    run_id text NOT NULL, source_id text NOT NULL, seg integer NOT NULL, conn_gen integer NOT NULL,
    start_wall double precision NOT NULL, start_reason text NOT NULL,
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

CREATE TABLE IF NOT EXISTS commit_group (
    group_id text PRIMARY KEY, order_no bigint NOT NULL, run_id text NOT NULL, source_id text NOT NULL,
    kind text NOT NULL, records integer NOT NULL, built_wall double precision NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commit_group_order ON commit_group(run_id, order_no);

-- status_code is bigint: OPC UA status codes are unsigned 32-bit.  effective_wall is the time the
-- value counts from in history: the receipt time, or the assertion time for a C2 state assertion.
CREATE TABLE IF NOT EXISTS observation (
    id text PRIMARY KEY, group_id text NOT NULL,
    run_id text NOT NULL, source_id text NOT NULL, conn_gen integer NOT NULL, seg integer NOT NULL,
    batch_seq bigint NOT NULL, item_seq integer NOT NULL,
    point_id text NOT NULL, value double precision, quality text NOT NULL, status_code bigint,
    received_wall double precision NOT NULL, received_mono double precision NOT NULL,
    source_time double precision,
    origin text NOT NULL, orig_id text, asserted_wall double precision, asserted_mono double precision,
    extreme boolean NOT NULL DEFAULT false, tick bigint, poll_id text, response_id text,
    effective_wall double precision GENERATED ALWAYS AS (coalesce(asserted_wall, received_wall)) STORED
);
CREATE INDEX IF NOT EXISTS observation_point_time ON observation(point_id, effective_wall, batch_seq, item_seq);
CREATE INDEX IF NOT EXISTS observation_batch ON observation(run_id, source_id, batch_seq, item_seq);
CREATE INDEX IF NOT EXISTS observation_segment ON observation(run_id, source_id, seg, point_id);
CREATE INDEX IF NOT EXISTS observation_group ON observation(group_id);

CREATE TABLE IF NOT EXISTS result (
    id text PRIMARY KEY, group_id text NOT NULL,
    run_id text NOT NULL, source_id text NOT NULL, conn_gen integer NOT NULL, seg integer NOT NULL,
    batch_seq bigint NOT NULL,
    point_id text NOT NULL, value double precision, status text NOT NULL, reason text,
    extreme boolean NOT NULL DEFAULT false, config_id text NOT NULL REFERENCES config_version(id),
    at_wall double precision NOT NULL, basis jsonb, estimate jsonb,
    origin text NOT NULL, orig_id text, asserted_wall double precision,
    basis_incomplete boolean NOT NULL DEFAULT false,
    effective_wall double precision GENERATED ALWAYS AS (coalesce(asserted_wall, at_wall)) STORED
);
CREATE INDEX IF NOT EXISTS result_point_time ON result(point_id, effective_wall, batch_seq);
CREATE INDEX IF NOT EXISTS result_batch ON result(run_id, source_id, batch_seq);
CREATE INDEX IF NOT EXISTS result_group ON result(group_id);

CREATE TABLE IF NOT EXISTS state_event (
    id text PRIMARY KEY, group_id text NOT NULL, run_id text NOT NULL, source_id text NOT NULL,
    seg integer NOT NULL, kind text NOT NULL, at_wall double precision NOT NULL,
    at_mono double precision NOT NULL, detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
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
    id integer PRIMARY KEY CHECK(id=1), mode text NOT NULL,
    external_selected boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT now()
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
