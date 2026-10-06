-- Public note projection carries each indicator's deterministic reference provenance
-- (full count, SHA-256 of the full sorted list, truncation flag) so a bounded list of
-- exemplar sources is never shown as the complete list. Older notes without these
-- optional fields are returned unchanged. Simulated workload data only.
CREATE OR REPLACE FUNCTION reckoner.v1_public_note(doc jsonb) RETURNS jsonb
 LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,reckoner AS $$
 SELECT CASE WHEN doc IS NULL OR doc='null'::jsonb THEN NULL ELSE
 reckoner.v1_pick(doc,ARRAY['note_id','case_id','decision_id','evidence_id','prompt_version',
 'requested_model','reported_model','generation_status','started_at','completed_at',
 'verdict_recommendation']) || jsonb_build_object(
 'confidence',reckoner.v1_pick(doc->'confidence',ARRAY['value','meaning']),
 'entity_neighbourhood',reckoner.v1_pick(doc->'entity_neighbourhood',ARRAY['summary','evidence_refs']),
 'risk_indicators',(SELECT COALESCE(jsonb_agg(reckoner.v1_pick(value,ARRAY['rank','indicator_id','description','method','evidence_refs',
 'evidence_ref_count','evidence_refs_sha256','evidence_refs_truncated'])),'[]') FROM jsonb_array_elements(doc->'risk_indicators') WITH ORDINALITY a(value,n) WHERE n<=3),
 'comparable_cases',(SELECT COALESCE(jsonb_agg(reckoner.v1_pick(value,ARRAY['transaction_id','summary','evidence_refs'])),'[]') FROM jsonb_array_elements(doc->'comparable_cases') WITH ORDINALITY a(value,n) WHERE n<=20),
 'what_would_change_verdict',(SELECT COALESCE(jsonb_agg(reckoner.v1_pick(value,ARRAY['action','evidence_refs'])),'[]') FROM jsonb_array_elements(doc->'what_would_change_verdict') WITH ORDINALITY a(value,n) WHERE n<=20)) END
$$;
