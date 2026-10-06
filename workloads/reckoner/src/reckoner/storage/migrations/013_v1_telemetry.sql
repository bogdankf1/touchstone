ALTER TABLE reckoner.v1_runs ADD COLUMN telemetry_mode text NOT NULL DEFAULT 'workflow'
 CHECK (telemetry_mode IN ('workflow','scoring-only'));

-- Generic OTLP delivery is separate from immutable operational review source bytes.
CREATE TABLE reckoner.v1_otlp_delivery (
 tenant_id text NOT NULL,
 event_id text NOT NULL,
 run_id text NOT NULL,
 task_id text,
 document jsonb NOT NULL,
 payload bytea NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 delivered_at timestamptz,
 PRIMARY KEY (tenant_id,event_id),
 FOREIGN KEY (tenant_id,run_id) REFERENCES reckoner.v1_runs(tenant_id,run_id),
 CHECK (document->>'tenant_id'=tenant_id AND document->>'run_id'=run_id
   AND document->>'event_id'=event_id)
);
CREATE FUNCTION reckoner.v1_otlp_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR (to_jsonb(NEW)-'delivered_at') IS DISTINCT FROM (to_jsonb(OLD)-'delivered_at') THEN
   RAISE EXCEPTION 'immutable OTLP delivery identity' USING ERRCODE='check_violation';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_otlp BEFORE UPDATE OR DELETE ON reckoner.v1_otlp_delivery
 FOR EACH ROW EXECUTE FUNCTION reckoner.v1_otlp_immutable();
GRANT SELECT,INSERT ON reckoner.v1_otlp_delivery TO reckoner_runner,reckoner_evaluator;
GRANT UPDATE(delivered_at) ON reckoner.v1_otlp_delivery TO reckoner_runner,reckoner_evaluator;
ALTER TABLE reckoner.v1_note_evaluations ADD COLUMN recorded_at timestamptz NOT NULL DEFAULT clock_timestamp();
CREATE FUNCTION reckoner.v1_cost_scope(purpose text) RETURNS text
 LANGUAGE sql IMMUTABLE AS $$
 SELECT CASE WHEN purpose IN ('final','online-note','fabricated') THEN 'online'
             WHEN purpose IN ('pilot','calibration','development','validation','graph-comparison','judge') THEN 'offline'
        END
$$;
CREATE FUNCTION reckoner.v1_guard_closed_calls() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM task_id FROM reckoner.v1_tasks WHERE tenant_id=NEW.tenant_id
   AND run_id=NEW.run_id AND task_id=NEW.task_id FOR UPDATE;
 IF reckoner.v1_cost_scope(NEW.purpose) IS NULL OR EXISTS (
   SELECT 1 FROM reckoner.v1_otlp_delivery WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
   AND task_id=NEW.task_id AND document->>'event_kind'='work_closure'
   AND document->'payload'->>'cost_scope'=reckoner.v1_cost_scope(NEW.purpose)) THEN
   RAISE EXCEPTION 'cost scope closed or unknown' USING ERRCODE='check_violation';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER closed_call_guard BEFORE INSERT ON reckoner.v1_provider_calls
 FOR EACH ROW EXECUTE FUNCTION reckoner.v1_guard_closed_calls();
CREATE FUNCTION reckoner.v1_guard_closure() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE scope text; actual jsonb;
BEGIN
 IF NEW.document->>'event_kind' IS DISTINCT FROM 'work_closure' THEN RETURN NEW; END IF;
 scope := NEW.document->'payload'->>'cost_scope';
 PERFORM task_id FROM reckoner.v1_tasks WHERE tenant_id=NEW.tenant_id
   AND run_id=NEW.run_id AND task_id=NEW.task_id FOR UPDATE;
 IF NOT EXISTS (SELECT 1 FROM reckoner.v1_decisions WHERE tenant_id=NEW.tenant_id
   AND run_id=NEW.run_id AND task_id=NEW.task_id) THEN
   IF NOT EXISTS (SELECT 1 FROM reckoner.v1_runs WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
       AND telemetry_mode='scoring-only' AND purpose IN ('pilot','calibration','development','validation','graph-comparison'))
     OR NOT EXISTS (SELECT 1 FROM reckoner.v1_provider_calls WHERE tenant_id=NEW.tenant_id
       AND run_id=NEW.run_id AND task_id=NEW.task_id)
     OR EXISTS (SELECT 1 FROM reckoner.v1_provider_calls c WHERE c.tenant_id=NEW.tenant_id
       AND c.run_id=NEW.run_id AND c.task_id=NEW.task_id
       AND NOT EXISTS (SELECT 1 FROM reckoner.v1_protocol_closures p WHERE p.protocol_id=c.protocol_id)) THEN
     RAISE EXCEPTION 'pending root work or source protocol' USING ERRCODE='check_violation';
   END IF;
 END IF;
 IF EXISTS (SELECT 1 FROM reckoner.v1_note_work w LEFT JOIN reckoner.v1_note_results r
   USING(tenant_id,case_id) WHERE w.tenant_id=NEW.tenant_id AND w.run_id=NEW.run_id
   AND w.task_id=NEW.task_id AND r.case_id IS NULL) THEN
   RAISE EXCEPTION 'pending note work' USING ERRCODE='check_violation';
 END IF;
 SELECT coalesce(jsonb_agg(call_id ORDER BY call_id),'[]'::jsonb) INTO actual
   FROM reckoner.v1_provider_calls WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
   AND task_id=NEW.task_id AND reckoner.v1_cost_scope(purpose)=scope;
 IF actual IS DISTINCT FROM NEW.document->'payload'->'call_ids' OR EXISTS (
   SELECT 1 FROM reckoner.v1_provider_calls c WHERE c.tenant_id=NEW.tenant_id
   AND c.run_id=NEW.run_id AND c.task_id=NEW.task_id AND reckoner.v1_cost_scope(c.purpose)=scope
   AND NOT EXISTS (SELECT 1 FROM reckoner.v1_settlements s WHERE s.tenant_id=c.tenant_id
     AND s.call_id=c.call_id AND s.status='settled')) THEN
   RAISE EXCEPTION 'pending or mismatched billing closure' USING ERRCODE='check_violation';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER closure_guard BEFORE INSERT ON reckoner.v1_otlp_delivery
 FOR EACH ROW EXECUTE FUNCTION reckoner.v1_guard_closure();
