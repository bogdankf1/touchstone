-- Simulated-workload note readiness is separate from immutable root decisions.
CREATE TABLE reckoner.v1_note_work (
 tenant_id text NOT NULL, case_id text NOT NULL, run_id text NOT NULL, task_id text NOT NULL,
 document jsonb NOT NULL, PRIMARY KEY(tenant_id,case_id),
 FOREIGN KEY(tenant_id,case_id) REFERENCES reckoner.v1_cases(tenant_id,case_id),
 FOREIGN KEY(tenant_id,run_id,task_id) REFERENCES reckoner.v1_tasks(tenant_id,run_id,task_id)
);
CREATE TABLE reckoner.v1_note_results (
 tenant_id text NOT NULL, case_id text NOT NULL, document jsonb NOT NULL,
 PRIMARY KEY(tenant_id,case_id),
 FOREIGN KEY(tenant_id,case_id) REFERENCES reckoner.v1_note_work(tenant_id,case_id)
);
CREATE TABLE reckoner.v1_generation_stages (
 tenant_id text NOT NULL, run_id text NOT NULL, task_id text NOT NULL,
 stage text NOT NULL, call_id text NOT NULL, document jsonb NOT NULL,
 PRIMARY KEY(tenant_id,run_id,task_id,stage),
 FOREIGN KEY(tenant_id,call_id) REFERENCES reckoner.v1_provider_calls(tenant_id,call_id),
 FOREIGN KEY(tenant_id,run_id,task_id) REFERENCES reckoner.v1_tasks(tenant_id,run_id,task_id)
);
CREATE TABLE reckoner.v1_note_evaluations (
 tenant_id text NOT NULL, evaluation_id text NOT NULL, run_id text NOT NULL,
 document jsonb NOT NULL, PRIMARY KEY(tenant_id,evaluation_id),
 FOREIGN KEY(tenant_id,run_id) REFERENCES reckoner.v1_runs(tenant_id,run_id)
);
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['v1_note_work','v1_note_results','v1_generation_stages','v1_note_evaluations'] LOOP
  EXECUTE format('CREATE TRIGGER note_immutable BEFORE UPDATE OR DELETE ON reckoner.%I FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable()',name);
 END LOOP;
END $$;
GRANT SELECT,INSERT ON reckoner.v1_note_work,reckoner.v1_note_results,
 reckoner.v1_generation_stages,reckoner.v1_note_evaluations TO reckoner_runner;
GRANT SELECT ON reckoner.v1_note_work,reckoner.v1_note_results,
 reckoner.v1_generation_stages,reckoner.v1_note_evaluations TO reckoner_evaluator;
CREATE VIEW reckoner.api_v1_note_work AS SELECT tenant_id,case_id,run_id,task_id,document FROM reckoner.v1_note_work;
CREATE VIEW reckoner.api_v1_note_results AS SELECT tenant_id,case_id,document FROM reckoner.v1_note_results;
GRANT SELECT ON reckoner.api_v1_note_work,reckoner.api_v1_note_results TO reckoner_api;
-- Row lock only: note writers receive no case UPDATE privilege.
CREATE FUNCTION reckoner.v1_lock_note_case(p_tenant text,p_case text) RETURNS text
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,reckoner AS $$
 DECLARE result text; BEGIN
  SELECT status INTO STRICT result FROM reckoner.v1_cases
   WHERE tenant_id=p_tenant AND case_id=p_case FOR UPDATE;
  RETURN result;
 END $$;
REVOKE ALL ON FUNCTION reckoner.v1_lock_note_case(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reckoner.v1_lock_note_case(text,text) TO reckoner_runner;
ALTER TABLE reckoner.v1_note_work ADD CHECK (
 document->>'tenant_id'=tenant_id AND document->>'case_id'=case_id
 AND document->>'run_id'=run_id AND document->>'task_id'=task_id);
ALTER TABLE reckoner.v1_note_results ADD CHECK (
 document->>'tenant_id'=tenant_id AND document->>'case_id'=case_id);
ALTER TABLE reckoner.v1_generation_stages ADD CHECK (
 document->>'tenant_id'=tenant_id AND document->>'run_id'=run_id
 AND document->>'task_id'=task_id AND document->>'stage'=stage
 AND document->>'call_id'=call_id);
ALTER TABLE reckoner.v1_note_evaluations ADD CHECK (
 document->>'tenant_id'=tenant_id AND document->>'run_id'=run_id
 AND document->>'evaluation_id'=evaluation_id);
