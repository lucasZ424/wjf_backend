CREATE TABLE IF NOT EXISTS config_snapshot (
    id text PRIMARY KEY, payload jsonb NOT NULL, saved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS event (
    id text PRIMARY KEY, profile_id text NOT NULL, phase text NOT NULL,
    payload jsonb NOT NULL, saved_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS event_saved ON event(saved_at DESC, id);
CREATE TABLE IF NOT EXISTS source_sample (
    id text PRIMARY KEY, point_id text NOT NULL, effective_time double precision NOT NULL,
    payload jsonb NOT NULL, saved_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS source_point_time ON source_sample(point_id, effective_time, id);
CREATE TABLE IF NOT EXISTS event_sample (
    event_id text NOT NULL, sample_id text NOT NULL REFERENCES source_sample(id),
    PRIMARY KEY(event_id, sample_id)
);
CREATE TABLE IF NOT EXISTS frame (
    id text PRIMARY KEY, run_id text NOT NULL, window_start double precision NOT NULL,
    window_end double precision NOT NULL, config_id text NOT NULL,
    partial boolean NOT NULL, saved_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS frame_time ON frame(window_start, id);
CREATE TABLE IF NOT EXISTS frame_value (
    ingest_id bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    frame_id text NOT NULL REFERENCES frame(id), point_id text NOT NULL,
    product text NOT NULL CHECK(product IN ('source','derived')), payload jsonb NOT NULL,
    PRIMARY KEY(frame_id, point_id, product)
);
CREATE INDEX IF NOT EXISTS frame_value_seed ON frame_value(point_id, product, ingest_id DESC);
CREATE TABLE IF NOT EXISTS event_frame (
    event_id text NOT NULL, frame_id text NOT NULL REFERENCES frame(id),
    recomputable boolean NOT NULL DEFAULT false, results_removed boolean NOT NULL DEFAULT false,
    PRIMARY KEY(event_id, frame_id)
);
CREATE TABLE IF NOT EXISTS trigger_fact (
    id text PRIMARY KEY, event_id text NOT NULL, payload jsonb NOT NULL,
    saved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS gap (
    id text PRIMARY KEY, event_id text, payload jsonb NOT NULL, saved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS routing_state (
    id integer PRIMARY KEY CHECK(id=1), mode text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE routing_state ADD COLUMN IF NOT EXISTS external_selected boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS account (
    username text PRIMARY KEY, role text NOT NULL CHECK(role IN ('admin','viewer')),
    salt text NOT NULL, digest text NOT NULL, active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY);
INSERT INTO schema_version VALUES(1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS write_probe (id integer PRIMARY KEY CHECK(id=1), touched_at timestamptz NOT NULL);
ALTER TABLE frame_value ADD COLUMN IF NOT EXISTS source_seq bigint;
ALTER TABLE frame_value ADD COLUMN IF NOT EXISTS good_prefix bigint;
CREATE UNIQUE INDEX IF NOT EXISTS source_sequence_key ON frame_value(point_id,source_seq) WHERE product='source';
CREATE TABLE IF NOT EXISTS source_counter (
    point_id text PRIMARY KEY, last_seq bigint NOT NULL, total_good bigint NOT NULL,
    retained bigint NOT NULL, retained_good bigint NOT NULL
);
-- One-time upgrade: existing source rows establish the initial committed order.
DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM schema_version WHERE version=2) THEN
        WITH numbered AS (
            SELECT ingest_id, row_number() OVER(PARTITION BY point_id ORDER BY ingest_id) AS n,
            sum(CASE WHEN payload->>'quality'='GOOD' THEN 1 ELSE 0 END)
                OVER(PARTITION BY point_id ORDER BY ingest_id) AS g
            FROM frame_value WHERE product='source'
        ) UPDATE frame_value v SET source_seq=n.n,good_prefix=n.g FROM numbered n WHERE n.ingest_id=v.ingest_id;
        INSERT INTO source_counter SELECT point_id,max(source_seq),max(good_prefix),count(*),
            count(*) FILTER(WHERE payload->>'quality'='GOOD') FROM frame_value WHERE product='source' GROUP BY point_id
            ON CONFLICT(point_id) DO NOTHING;
        INSERT INTO schema_version VALUES(2);
    END IF;
END $$;
CREATE OR REPLACE FUNCTION maintain_source_counter() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n bigint; g bigint; good integer;
BEGIN
    IF TG_OP='INSERT' THEN
        good := CASE WHEN NEW.payload->>'quality'='GOOD' THEN 1 ELSE 0 END;
        INSERT INTO source_counter VALUES(NEW.point_id,1,good,1,good)
            ON CONFLICT(point_id) DO UPDATE SET last_seq=source_counter.last_seq+1,
            total_good=source_counter.total_good+good,retained=source_counter.retained+1,
            retained_good=source_counter.retained_good+good
            RETURNING last_seq,total_good INTO n,g;
        UPDATE frame_value SET source_seq=n,good_prefix=g WHERE ingest_id=NEW.ingest_id;
        RETURN NEW;
    END IF;
    good := CASE WHEN OLD.payload->>'quality'='GOOD' THEN 1 ELSE 0 END;
    UPDATE source_counter SET retained=retained-1,retained_good=retained_good-good WHERE point_id=OLD.point_id;
    RETURN OLD;
END $$;
CREATE OR REPLACE TRIGGER source_insert_count AFTER INSERT ON frame_value
    FOR EACH ROW WHEN (NEW.product='source') EXECUTE FUNCTION maintain_source_counter();
CREATE OR REPLACE TRIGGER source_delete_count AFTER DELETE ON frame_value
    FOR EACH ROW WHEN (OLD.product='source') EXECUTE FUNCTION maintain_source_counter();
CREATE INDEX IF NOT EXISTS source_expiry ON source_sample(saved_at,id);
CREATE INDEX IF NOT EXISTS event_sample_reverse ON event_sample(sample_id,event_id);
CREATE INDEX IF NOT EXISTS event_frame_reverse ON event_frame(frame_id,event_id);
CREATE INDEX IF NOT EXISTS frame_saved ON frame(saved_at DESC);
CREATE INDEX IF NOT EXISTS event_query_order ON event((coalesce((payload->>'triggered_at')::double precision,0)),id);
CREATE INDEX IF NOT EXISTS trigger_query_order ON trigger_fact(event_id,(coalesce((payload->>'time')::double precision,0)),id);
CREATE INDEX IF NOT EXISTS gap_query_order ON gap((coalesce((payload->>'start')::double precision,0)),id);

CREATE INDEX IF NOT EXISTS frame_end ON frame(window_end,id);
CREATE INDEX IF NOT EXISTS gap_event ON gap(event_id);

CREATE INDEX IF NOT EXISTS source_time_order ON source_sample(effective_time,id);

CREATE TABLE IF NOT EXISTS management_log (
    id text PRIMARY KEY, time double precision NOT NULL, actor text NOT NULL,
    action text NOT NULL, target text NOT NULL, result text NOT NULL
);
CREATE INDEX IF NOT EXISTS management_log_time ON management_log(time DESC,id DESC);
INSERT INTO schema_version VALUES(3) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS fault_point (
    id text PRIMARY KEY, point_id text NOT NULL, time double precision NOT NULL,
    payload jsonb NOT NULL, saved_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fault_time ON fault_point(time DESC,id DESC);
CREATE INDEX IF NOT EXISTS fault_channel_time ON fault_point(point_id,time DESC,id DESC);
INSERT INTO schema_version VALUES(4) ON CONFLICT DO NOTHING;
