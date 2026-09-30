-- Local simulated workload operational capabilities. Identity is attribution, not auth.
CREATE TABLE reckoner.v1_model_registry (
 provider text NOT NULL, model text NOT NULL, purpose text NOT NULL,
 contract_supported boolean NOT NULL, price_table jsonb NOT NULL,
 PRIMARY KEY(provider,model,purpose)
);
CREATE TABLE reckoner.v1_configuration_details (
 tenant_id text NOT NULL, config_id text NOT NULL, document jsonb NOT NULL,
 PRIMARY KEY(tenant_id,config_id),
 FOREIGN KEY(tenant_id,config_id) REFERENCES reckoner.v1_configs(tenant_id,config_id)
);
CREATE TABLE reckoner.v1_active_configuration (
 tenant_id text PRIMARY KEY, config_id text, version bigint NOT NULL DEFAULT 0,
 FOREIGN KEY(tenant_id,config_id) REFERENCES reckoner.v1_configs(tenant_id,config_id)
);
CREATE TABLE reckoner.v1_configuration_activations (
 tenant_id text NOT NULL, idempotency_key text NOT NULL, config_id text NOT NULL,
 expected_version bigint NOT NULL, document jsonb NOT NULL,
 PRIMARY KEY(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id,config_id) REFERENCES reckoner.v1_configs(tenant_id,config_id)
);
CREATE TRIGGER no_config_delete BEFORE DELETE ON reckoner.v1_configs
 FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable();
CREATE TRIGGER threshold_immutable BEFORE UPDATE OR DELETE ON reckoner.threshold_configs
 FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable();
CREATE TRIGGER details_immutable BEFORE UPDATE OR DELETE ON reckoner.v1_configuration_details
 FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable();
CREATE TRIGGER activations_immutable BEFORE UPDATE OR DELETE ON reckoner.v1_configuration_activations
 FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable();
CREATE TRIGGER reviews_immutable BEFORE UPDATE OR DELETE ON reckoner.v1_reviews
 FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable();

CREATE FUNCTION reckoner.v1_pick(doc jsonb, keys text[]) RETURNS jsonb
 LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT COALESCE(jsonb_object_agg(key,value),'{}'::jsonb) FROM jsonb_each(doc)
 WHERE key=ANY(keys)
$$;
-- Only structured note content, never raw attempts/requests/responses.
CREATE FUNCTION reckoner.v1_public_note(doc jsonb) RETURNS jsonb
 LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,reckoner AS $$
 SELECT CASE WHEN doc IS NULL OR doc='null'::jsonb THEN NULL ELSE
 reckoner.v1_pick(doc,ARRAY['note_id','case_id','decision_id','evidence_id','prompt_version',
 'requested_model','reported_model','generation_status','started_at','completed_at',
 'verdict_recommendation']) || jsonb_build_object(
 'confidence',reckoner.v1_pick(doc->'confidence',ARRAY['value','meaning']),
 'entity_neighbourhood',reckoner.v1_pick(doc->'entity_neighbourhood',ARRAY['summary','evidence_refs']),
 'risk_indicators',(SELECT COALESCE(jsonb_agg(reckoner.v1_pick(value,ARRAY['rank','indicator_id','description','method','evidence_refs'])),'[]') FROM jsonb_array_elements(doc->'risk_indicators') WITH ORDINALITY a(value,n) WHERE n<=3),
 'comparable_cases',(SELECT COALESCE(jsonb_agg(reckoner.v1_pick(value,ARRAY['transaction_id','summary','evidence_refs'])),'[]') FROM jsonb_array_elements(doc->'comparable_cases') WITH ORDINALITY a(value,n) WHERE n<=20),
 'what_would_change_verdict',(SELECT COALESCE(jsonb_agg(reckoner.v1_pick(value,ARRAY['action','evidence_refs'])),'[]') FROM jsonb_array_elements(doc->'what_would_change_verdict') WITH ORDINALITY a(value,n) WHERE n<=20)) END
$$;
CREATE VIEW reckoner.api_v1_case_details AS
SELECT c.tenant_id,c.case_id,c.decision_id,c.version,c.created_at,d.run_id,d.task_id,
 CASE WHEN c.status='reviewed' THEN 'resolved' ELSE 'open' END AS status,
 COALESCE(n.document->>'status','pending') AS note_status,
 (d.scorer_status<>'succeeded' OR e.coverage_status<>'available') AS degraded,
 d.document->>'degraded_reason' AS degraded_reason,
 d.transaction_id,d.config_id,d.evidence_id,
 t.document->>'amount_minor' AS amount_minor,t.document->>'currency' AS currency,
 t.document->>'occurred_at' AS occurred_at,
 d.document->>'raw_probability' AS raw_probability,
 d.document->>'effective_probability' AS effective_probability,
 d.document->>'effective_low_threshold' AS effective_low_threshold,
 d.document->>'effective_high_threshold' AS effective_high_threshold,
 e.document->'coverage' AS coverage,e.document->'cutoffs' AS cutoffs,
 e.document->'source_snapshot_ids' AS source_snapshot_ids,
 jsonb_build_object('status',CASE WHEN e.document->'neighbourhood' IS NULL THEN 'unavailable' ELSE 'available' END,
 'total_nodes',COALESCE(e.document#>'{neighbourhood,total_nodes}','0'),
 'total_edges',COALESCE(e.document#>'{neighbourhood,total_edges}','0'),
 'truncated',(COALESCE((e.document#>>'{neighbourhood,truncated}')::boolean,false) OR COALESCE(jsonb_array_length(e.document#>'{neighbourhood,nodes}'),0)>100 OR COALESCE(jsonb_array_length(e.document#>'{neighbourhood,edges}'),0)>200),
 'cross_tenant',COALESCE(e.document#>'{neighbourhood,cross_tenant}','false'),
 'nodes',(SELECT COALESCE(jsonb_agg(reckoner.v1_pick(value,ARRAY['id','tenant_id','kind','identity'])),'[]') FROM jsonb_array_elements(e.document#>'{neighbourhood,nodes}') WITH ORDINALITY a(value,n) WHERE n<=100),
 'edges',(SELECT COALESCE(jsonb_agg(reckoner.v1_pick(value,ARRAY['id','source','target','kind'])),'[]') FROM jsonb_array_elements(e.document#>'{neighbourhood,edges}') WITH ORDINALITY a(value,n) WHERE n<=200)) AS graph,
 reckoner.v1_public_note(n.document->'note') AS note,
 (n.document->>'available_before_review')::boolean AS available_before_review,
 CASE WHEN r.document IS NULL THEN NULL ELSE reckoner.v1_pick(r.document,
 ARRAY['action_id','case_id','decision_id','reviewer_id','reviewer_type','verdict','recommendation',
 'prior_case_version','reviewed_at','idempotency_key']) END AS review
FROM reckoner.v1_cases c JOIN reckoner.v1_decisions d USING(tenant_id,decision_id)
 JOIN reckoner.transactions t ON (t.tenant_id,t.transaction_id)=(d.tenant_id,d.transaction_id)
 JOIN reckoner.v1_evidence e ON (e.tenant_id,e.evidence_id)=(d.tenant_id,d.evidence_id)
 LEFT JOIN reckoner.v1_note_results n ON (n.tenant_id,n.case_id)=(c.tenant_id,c.case_id)
 LEFT JOIN reckoner.v1_reviews r ON (r.tenant_id,r.case_id)=(c.tenant_id,c.case_id);
-- Close older broad note-result projections now that operational reads are available.
REVOKE SELECT ON reckoner.api_v1_note_results,reckoner.api_v1_note_work FROM reckoner_api;
CREATE OR REPLACE VIEW reckoner.api_v1_notes AS
 SELECT tenant_id,note_id,case_id,decision_id,generation_status,
 reckoner.v1_public_note(document) AS document,completed_at FROM reckoner.v1_notes;
CREATE VIEW reckoner.api_v1_run_policy AS
 SELECT tenant_id,run_id,document->>'evidence_mode' AS evidence_mode,
 document->>'data_kind' AS data_kind FROM reckoner.v1_workflow_runs;
GRANT SELECT ON reckoner.api_v1_run_policy TO reckoner_api;
CREATE VIEW reckoner.api_v1_configurations AS
 SELECT c.tenant_id,c.config_id,c.document AS configuration,t.document AS thresholds,
 CASE WHEN x.document IS NULL THEN NULL ELSE jsonb_build_object('evidence_mode',x.document->'evidence_mode','data_kind',x.document->'data_kind','calibration',CASE WHEN x.document->'calibration' IS NULL OR x.document->'calibration'='null'::jsonb THEN NULL ELSE jsonb_build_object('calibration_id',x.document#>'{calibration,calibration_id}','fit_status',x.document#>'{calibration,fit_status}','qualification',reckoner.v1_pick(x.document#>'{calibration,qualification}',ARRAY['status','validation_id','raw_brier','candidate_brier','raw_log_loss','candidate_log_loss'])) END) END AS qualification FROM reckoner.v1_configs c
 JOIN reckoner.threshold_configs t ON (t.tenant_id,t.config_id)=(c.tenant_id,c.threshold_config_id)
 LEFT JOIN reckoner.v1_configuration_details x ON (x.tenant_id,x.config_id)=(c.tenant_id,c.config_id);
CREATE VIEW reckoner.api_v1_activation AS SELECT * FROM reckoner.v1_active_configuration;
CREATE VIEW reckoner.api_v1_models AS SELECT provider,model,purpose,price_table
 FROM reckoner.v1_model_registry WHERE contract_supported;
CREATE VIEW reckoner.api_v1_preview AS
 SELECT d.tenant_id,d.run_id,d.task_id,d.config_id,d.document->>'effective_probability' AS probability,
 t.document->>'amount_minor' AS amount_minor FROM reckoner.v1_decisions d
 JOIN reckoner.transactions t USING(tenant_id,transaction_id);

CREATE FUNCTION reckoner.v1_review(p_tenant text,p_case text,p_actor text,p_verdict text,
 p_key text,p_version integer,p_simulated boolean DEFAULT false) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,reckoner AS $$
DECLARE c reckoner.v1_cases; d reckoner.v1_decisions; previous jsonb; doc jsonb;
 action text; event text; event_doc jsonb; recommendation text; oracle_id text; policy text;
 actual_verdict text; actor text; kind text; reviewed timestamptz;
BEGIN
 IF p_simulated AND NOT pg_has_role(session_user,'reckoner_evaluator','member') THEN
  RAISE EXCEPTION 'simulation requires evaluator' USING ERRCODE='insufficient_privilege'; END IF;
 IF p_tenant IS NULL OR p_case IS NULL OR p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 256
 OR p_version IS NULL OR p_version<1 OR p_actor IS NULL OR length(p_actor) NOT BETWEEN 1 AND 256
 OR p_verdict IS NULL OR p_verdict NOT IN ('approve','decline')
 OR (NOT p_simulated AND lower(p_actor) LIKE 'simulat%%') THEN
  RAISE EXCEPTION 'invalid review' USING ERRCODE='check_violation'; END IF;
 SELECT * INTO c FROM reckoner.v1_cases WHERE tenant_id=p_tenant AND case_id=p_case FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'case not found' USING ERRCODE='no_data_found'; END IF;
 SELECT * INTO STRICT d FROM reckoner.v1_decisions WHERE tenant_id=p_tenant AND decision_id=c.decision_id;
 actor:=p_actor; actual_verdict:=p_verdict; kind:='human';
 IF p_simulated THEN
  SELECT o.oracle_version,CASE WHEN o.label='fraud' THEN 'decline' ELSE 'approve' END
   INTO STRICT oracle_id,actual_verdict FROM oracle.oracle_labels o
   JOIN reckoner.v1_runs run ON run.tenant_id=o.tenant_id AND run.run_id=d.run_id
   WHERE o.tenant_id=p_tenant AND o.transaction_id=d.transaction_id
   AND o.oracle_version='cctd-label-v1';
  SELECT document->>'resolution_policy_version' INTO policy FROM reckoner.v1_configs
   WHERE tenant_id=p_tenant AND config_id=d.config_id;
  actor:='simulated-reviewer'; kind:='simulated';
 END IF;
 SELECT document INTO previous FROM reckoner.v1_reviews
  WHERE tenant_id=p_tenant AND case_id=p_case AND idempotency_key=p_key;
 IF FOUND THEN
  IF previous->>'reviewer_id'<>actor OR previous->>'reviewer_type'<>kind
   OR previous->>'verdict'<>actual_verdict OR (previous->>'prior_case_version')::integer<>p_version THEN
   RAISE EXCEPTION 'idempotency conflict' USING ERRCODE='serialization_failure'; END IF;
  RETURN previous;
 END IF;
 IF c.status<>'pending' OR c.version<>p_version THEN
  RAISE EXCEPTION 'stale case version' USING ERRCODE='serialization_failure'; END IF;
 SELECT document->>'verdict_recommendation' INTO recommendation FROM reckoner.v1_notes
  WHERE tenant_id=p_tenant AND case_id=p_case AND generation_status IN ('succeeded','degraded')
  ORDER BY completed_at,note_id LIMIT 1;
 action:=encode(sha256(convert_to(jsonb_build_array('review-v1',p_tenant,p_case,p_key)::text,'UTF8')),'hex');
 event:=encode(sha256(convert_to(jsonb_build_array('review-event-v1',p_tenant,action)::text,'UTF8')),'hex');
 reviewed:=clock_timestamp();
 doc:=jsonb_build_object('schema_version','reckoner-review-v1','tenant_id',p_tenant,'case_id',p_case,
 'action_id',action,'decision_id',c.decision_id,'reviewer_id',actor,'reviewer_type',kind,
 'verdict',actual_verdict,'recommendation',recommendation,'idempotency_key',p_key,
 'prior_case_version',p_version,'reviewed_at',reviewed,'oracle_version',oracle_id,
 'resolution_policy_version',policy);
 INSERT INTO reckoner.v1_reviews VALUES(p_tenant,action,p_case,c.decision_id,p_key,p_version,kind,actual_verdict,doc,reviewed);
 UPDATE reckoner.v1_cases SET status='reviewed',version=version+1 WHERE tenant_id=p_tenant AND case_id=p_case;
 event_doc:=jsonb_build_object('schema_version','reckoner-review-outbox-v1','event_kind','review',
 'event_id',event,'tenant_id',p_tenant,'workflow_id','reckoner','workflow_version','reckoner-v1',
 'run_id',d.run_id,'task_id',d.task_id,'action_id',action,'case_id',p_case,
 'decision_id',c.decision_id,'config_id',d.config_id,'evidence_id',d.evidence_id,
 'reviewer_type',kind,'verdict',actual_verdict,'recommendation',recommendation,
 'reviewed_at',reviewed,'started_at',d.document->>'started_at','dataset_simulated',true);
 INSERT INTO reckoner.v1_outbox(tenant_id,event_id,run_id,task_id,document,payload,created_at)
 VALUES(p_tenant,event,d.run_id,d.task_id,event_doc,convert_to(event_doc::text,'UTF8'),reviewed);
 RETURN doc;
END $$;

-- Reuse selected immutable artifacts from saved settings or already-bound runs.
-- No HTTP route returns this internal artifact; each use revalidates its context.
CREATE FUNCTION reckoner.v1_configuration_calibration(p_tenant text,p_calibration text)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,reckoner AS $$
DECLARE artifact jsonb;
BEGIN
 SELECT DISTINCT source.document->'calibration' INTO STRICT artifact FROM (
  SELECT document FROM reckoner.v1_configuration_details WHERE tenant_id=p_tenant
  UNION ALL
  SELECT document FROM reckoner.v1_workflow_runs WHERE tenant_id=p_tenant
 ) source
 WHERE source.document#>>'{calibration,calibration_id}'=p_calibration
 AND source.document#>>'{calibration,qualification,status}'='selected';
 RETURN artifact;
 EXCEPTION WHEN no_data_found THEN RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION reckoner.v1_configuration_calibration(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reckoner.v1_configuration_calibration(text,text) TO reckoner_api;

CREATE FUNCTION reckoner.v1_save_configuration(c jsonb,t jsonb,q jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,reckoner AS $$
DECLARE part text; selected_model jsonb; saved jsonb;
BEGIN
 IF c->>'tenant_id' IS DISTINCT FROM t->>'tenant_id' OR c->>'threshold_config_id' IS DISTINCT FROM t->>'config_id' THEN
  RAISE EXCEPTION 'configuration ownership mismatch' USING ERRCODE='check_violation'; END IF;
 FOREACH part IN ARRAY ARRAY['scorer','note_model','judge_model'] LOOP
  selected_model:=c->part;
  IF NOT EXISTS (SELECT FROM reckoner.v1_model_registry r WHERE r.provider=selected_model->>'provider'
   AND r.model=selected_model->>'model' AND r.purpose=CASE WHEN part='scorer' THEN 'scorer' ELSE 'note' END
   AND r.contract_supported AND r.price_table=selected_model->'price_table') THEN
   RAISE EXCEPTION 'unsupported or unpriced model' USING ERRCODE='check_violation'; END IF;
 END LOOP;
 INSERT INTO reckoner.threshold_configs VALUES(t->>'tenant_id',t->>'config_id',t) ON CONFLICT DO NOTHING;
 SELECT document INTO saved FROM reckoner.threshold_configs WHERE tenant_id=t->>'tenant_id' AND config_id=t->>'config_id';
 IF saved IS DISTINCT FROM t THEN RAISE EXCEPTION 'immutable threshold conflict' USING ERRCODE='check_violation'; END IF;
 INSERT INTO reckoner.v1_configs VALUES(c->>'tenant_id',c->>'config_id',c->>'threshold_config_id',c->>'workflow_version',c) ON CONFLICT DO NOTHING;
 SELECT document INTO saved FROM reckoner.v1_configs WHERE tenant_id=c->>'tenant_id' AND config_id=c->>'config_id';
 IF saved IS DISTINCT FROM c THEN RAISE EXCEPTION 'immutable configuration conflict' USING ERRCODE='check_violation'; END IF;
 INSERT INTO reckoner.v1_configuration_details VALUES(c->>'tenant_id',c->>'config_id',q) ON CONFLICT DO NOTHING;
 SELECT document INTO saved FROM reckoner.v1_configuration_details WHERE tenant_id=c->>'tenant_id' AND config_id=c->>'config_id';
 IF saved IS DISTINCT FROM q THEN RAISE EXCEPTION 'immutable qualification conflict' USING ERRCODE='check_violation'; END IF;
END $$;
CREATE FUNCTION reckoner.v1_activate_configuration(p_tenant text,p_config text,p_version bigint,p_key text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,reckoner AS $$
DECLARE current_version bigint; previous reckoner.v1_configuration_activations; result jsonb;
BEGIN
 IF p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 256 OR p_version IS NULL OR p_version<0 THEN
  RAISE EXCEPTION 'invalid activation' USING ERRCODE='check_violation'; END IF;
 IF NOT EXISTS(SELECT FROM reckoner.v1_configuration_details WHERE tenant_id=p_tenant AND config_id=p_config) THEN
  RAISE EXCEPTION 'qualified configuration not found' USING ERRCODE='no_data_found'; END IF;
 INSERT INTO reckoner.v1_active_configuration(tenant_id) VALUES(p_tenant) ON CONFLICT DO NOTHING;
 SELECT version INTO current_version FROM reckoner.v1_active_configuration WHERE tenant_id=p_tenant FOR UPDATE;
 SELECT * INTO previous FROM reckoner.v1_configuration_activations WHERE tenant_id=p_tenant AND idempotency_key=p_key;
 IF FOUND THEN
  IF previous.config_id<>p_config OR previous.expected_version<>p_version THEN
   RAISE EXCEPTION 'idempotency conflict' USING ERRCODE='serialization_failure'; END IF;
  RETURN previous.document;
 END IF;
 IF current_version<>p_version THEN RAISE EXCEPTION 'stale activation version' USING ERRCODE='serialization_failure'; END IF;
 result:=jsonb_build_object('tenant_id',p_tenant,'config_id',p_config,'version',current_version+1);
 UPDATE reckoner.v1_active_configuration SET config_id=p_config,version=current_version+1 WHERE tenant_id=p_tenant;
 INSERT INTO reckoner.v1_configuration_activations VALUES(p_tenant,p_key,p_config,p_version,result);
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION reckoner.v1_review(text,text,text,text,text,integer,boolean),
 reckoner.v1_save_configuration(jsonb,jsonb,jsonb),reckoner.v1_activate_configuration(text,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reckoner.v1_review(text,text,text,text,text,integer,boolean) TO reckoner_api,reckoner_evaluator;
GRANT EXECUTE ON FUNCTION reckoner.v1_save_configuration(jsonb,jsonb,jsonb),reckoner.v1_activate_configuration(text,text,bigint,text) TO reckoner_api;
GRANT SELECT ON reckoner.api_v1_case_details,reckoner.api_v1_configurations,reckoner.api_v1_activation,
 reckoner.api_v1_models,reckoner.api_v1_preview TO reckoner_api,reckoner_evaluator;
INSERT INTO reckoner.v1_model_registry VALUES ('typesafe','jev-1.13.0','scorer',true,'{"schema_version": "price-table-v1", "model": "jev-1.13.0", "currency": "USD", "input_per_million": "0.042", "output_per_million": "0", "retrieved_at": "2026-09-30T00:00:00Z", "source_url": "https://docs.typesafe.ai/models", "price_table_version": "1c4fb24e0df14061bf8b1d90eab2db3102a753d352e89eca92be660f0a97a88c"}'::jsonb);
INSERT INTO reckoner.v1_model_registry VALUES ('anthropic','anthropic/claude-haiku-4-5-20251001','note',true,'{"currency": "USD", "input_per_million": "1.00", "model": "anthropic/claude-haiku-4-5-20251001", "output_per_million": "5.00", "price_table_version": "245d2d3dd12226e11e1328b65a7a63eb2db964b9e40a71eb56a0f30d07e6bcc2", "retrieved_at": "2026-09-25T00:00:00Z", "schema_version": "price-table-v1", "source_url": "https://platform.claude.com/docs/en/about-claude/pricing"}'::jsonb);
