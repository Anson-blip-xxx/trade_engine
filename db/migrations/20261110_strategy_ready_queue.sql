-- Additive ready projection. Keep canonical task history, decisions, receipts
-- and leases intact. Apply this DDL in a short-lock transaction, then backfill
-- in separate bounded transactions; enable only after exact set validation.
CREATE TABLE IF NOT EXISTS v2_strategy_ready (
    consumer TEXT NOT NULL,
    signal_id UUID NOT NULL,
    PRIMARY KEY (consumer, signal_id),
    FOREIGN KEY (consumer, signal_id)
        REFERENCES v2_strategy_tasks(consumer, signal_id)
        ON UPDATE CASCADE ON DELETE CASCADE
) WITH (autovacuum_vacuum_scale_factor=0, autovacuum_vacuum_threshold=1000);
CREATE TABLE IF NOT EXISTS v2_strategy_ready_control (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    enabled BOOLEAN NOT NULL DEFAULT false,
    validated_at TIMESTAMPTZ
);
-- Every migration run invalidates prior proof before restoring the trigger.
-- Otherwise a disabled trigger could have missed tasks while enabled stayed true.
INSERT INTO v2_strategy_ready_control(singleton) VALUES (true)
ON CONFLICT(singleton) DO UPDATE SET enabled=false,validated_at=NULL;

DO $migration$
DECLARE ns text := current_schema();
BEGIN
EXECUTE format($ddl$
CREATE OR REPLACE FUNCTION %1$I.v2_strategy_ready_sync_row() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
BEGIN
    IF TG_OP='DELETE' THEN
        DELETE FROM v2_strategy_ready
        WHERE (consumer,signal_id)=(OLD.consumer,OLD.signal_id);
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' AND (OLD.consumer,OLD.signal_id) IS DISTINCT FROM (NEW.consumer,NEW.signal_id) THEN
        DELETE FROM v2_strategy_ready
        WHERE (consumer,signal_id)=(OLD.consumer,OLD.signal_id);
    END IF;
    IF NEW.completed_at IS NULL THEN
        -- A membership-preserving lease/ACK does not rewrite ready rows.
        -- The probe also repairs an accidentally missing projection on update.
        IF NOT EXISTS (SELECT FROM v2_strategy_ready
                       WHERE (consumer,signal_id)=(NEW.consumer,NEW.signal_id)) THEN
            INSERT INTO v2_strategy_ready(consumer,signal_id)
            VALUES (NEW.consumer,NEW.signal_id) ON CONFLICT DO NOTHING;
        END IF;
    ELSE
        DELETE FROM v2_strategy_ready
        WHERE (consumer,signal_id)=(NEW.consumer,NEW.signal_id);
    END IF;
    RETURN NEW;
END;
$fn$;
$ddl$,ns);
IF NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid='v2_strategy_tasks'::regclass
               AND tgname='v2_strategy_ready_sync' AND NOT tgisinternal) THEN
    EXECUTE 'CREATE TRIGGER v2_strategy_ready_sync AFTER INSERT OR UPDATE OR DELETE
             ON v2_strategy_tasks FOR EACH ROW EXECUTE FUNCTION v2_strategy_ready_sync_row()';
END IF;
EXECUTE 'ALTER TABLE v2_strategy_tasks ENABLE ALWAYS TRIGGER v2_strategy_ready_sync';

EXECUTE format($ddl$
CREATE OR REPLACE FUNCTION %1$I.v2_strategy_ready_backfill(batch_limit integer DEFAULT 1000)
RETURNS integer LANGUAGE plpgsql SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
DECLARE copied integer;
BEGIN
    IF batch_limit IS NULL OR batch_limit<1 OR batch_limit>1000 THEN
        RAISE EXCEPTION 'READY_BACKFILL_LIMIT_INVALID';
    END IF;
    -- Lock canonical tasks first, just like the synchronizing trigger. No
    -- high-water cursor: skipped/in-flight rows are revisited on the next call.
    WITH missing AS MATERIALIZED (
        SELECT t.consumer,t.signal_id FROM v2_strategy_tasks t
        WHERE t.completed_at IS NULL
        AND NOT EXISTS (SELECT FROM v2_strategy_ready q
                        WHERE (q.consumer,q.signal_id)=(t.consumer,t.signal_id))
        ORDER BY t.consumer,t.signal_id LIMIT batch_limit FOR UPDATE OF t SKIP LOCKED
    )
    INSERT INTO v2_strategy_ready(consumer,signal_id)
    SELECT consumer,signal_id FROM missing ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS copied=ROW_COUNT;
    RETURN copied;
END;
$fn$;
$ddl$,ns);
EXECUTE format($ddl$
CREATE OR REPLACE FUNCTION %1$I.v2_strategy_ready_enable() RETURNS boolean
LANGUAGE plpgsql SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
BEGIN
    -- Short metadata handover; callers MUST set a bounded lock_timeout.
    -- Lock order matches task mutation -> projection mutation.
    LOCK TABLE v2_strategy_tasks IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE v2_strategy_ready IN SHARE ROW EXCLUSIVE MODE;
    IF NOT EXISTS (SELECT FROM pg_trigger
                   WHERE tgrelid='v2_strategy_tasks'::regclass
                   AND tgname='v2_strategy_ready_sync' AND tgenabled='A'
                   AND tgtype=29 AND tgqual IS NULL AND tgnargs=0 AND tgattr=''
                   AND tgfoid='v2_strategy_ready_sync_row()'::regprocedure
                   AND NOT tgisinternal) THEN
        RAISE EXCEPTION 'READY_SYNC_TRIGGER_INVALID';
    END IF;
    IF EXISTS (
        SELECT FROM v2_strategy_tasks t WHERE t.completed_at IS NULL
        AND NOT EXISTS (SELECT FROM v2_strategy_ready q
                        WHERE (q.consumer,q.signal_id)=(t.consumer,t.signal_id))
    ) OR EXISTS (
        SELECT FROM v2_strategy_ready q
        LEFT JOIN v2_strategy_tasks t
            ON (q.consumer,q.signal_id)=(t.consumer,t.signal_id)
        WHERE t.signal_id IS NULL OR t.completed_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'READY_PROJECTION_INCOMPLETE';
    END IF;
    UPDATE v2_strategy_ready_control SET enabled=true,validated_at=clock_timestamp()
    WHERE singleton;
    IF NOT FOUND THEN RAISE EXCEPTION 'READY_CONTROL_MISSING'; END IF;
    RETURN true;
END;
$fn$;
$ddl$,ns);
END;
$migration$;
REVOKE ALL ON v2_strategy_ready,v2_strategy_ready_control FROM PUBLIC;
REVOKE ALL ON FUNCTION v2_strategy_ready_backfill(integer),v2_strategy_ready_enable() FROM PUBLIC;
-- Fixed triggers do not drift with the amount of retained task history.
ALTER TABLE v2_strategy_tasks SET (
    autovacuum_vacuum_scale_factor=0, autovacuum_vacuum_threshold=100000
);
