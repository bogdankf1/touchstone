-- Simulated evidence: privileged preparation, cutoff-enforced runtime search.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE reckoner.v1_merchant_identities (
  tenant_id text NOT NULL, merchant_id text NOT NULL, identity text NOT NULL,
  PRIMARY KEY(tenant_id,merchant_id)
);
CREATE INDEX v1_shared_merchant ON reckoner.v1_merchant_identities(identity);
CREATE TABLE reckoner.v1_evidence_coverage (
  tenant_id text PRIMARY KEY, history_from timestamptz NOT NULL,
  history_until timestamptz NOT NULL, previous_card_complete boolean NOT NULL,
  source_snapshot_id text NOT NULL CHECK (source_snapshot_id ~ '^[a-f0-9]{64}$'),
  CHECK(history_from < history_until)
);
CREATE TABLE reckoner.v1_vectors (
  tenant_id text NOT NULL, transaction_id text NOT NULL, scaler_id text NOT NULL,
  feature_version text NOT NULL CHECK(feature_version='features-v1'),
  features vector(11) NOT NULL,
  PRIMARY KEY(tenant_id,transaction_id,scaler_id),
  FOREIGN KEY(tenant_id,transaction_id) REFERENCES reckoner.v1_history(tenant_id,transaction_id)
);
-- Exact L2 only; no approximate index.
CREATE FUNCTION reckoner.v1_neighbours(query_tenant text, query_transaction text)
RETURNS TABLE(document jsonb,merchant_identity text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog AS $$
  SELECT h.document,m.identity FROM reckoner.v1_history h
  JOIN reckoner.transactions q ON q.tenant_id=query_tenant AND q.transaction_id=query_transaction
  LEFT JOIN reckoner.v1_merchant_identities qm ON qm.tenant_id=query_tenant
    AND qm.merchant_id=q.document->>'merchant_id'
  LEFT JOIN reckoner.v1_merchant_identities m ON m.tenant_id=h.tenant_id AND m.merchant_id=h.merchant_id
  WHERE h.occurred_at < (q.document->>'occurred_at')::timestamptz
    AND h.occurred_at >= (q.document->>'occurred_at')::timestamptz - interval '30 days'
    AND ((h.tenant_id=query_tenant AND h.card_id=q.document->>'card_id')
      OR (qm.identity IS NOT NULL AND m.identity=qm.identity)
      OR (h.tenant_id=query_tenant AND h.merchant_id=q.document->>'merchant_id'))
  ORDER BY h.tenant_id,h.transaction_id
$$;
CREATE FUNCTION reckoner.v1_merchant_resolved(query_tenant text, query_transaction text)
RETURNS TABLE(document jsonb) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog AS $$
  SELECT r.document FROM oracle.v1_resolutions r
  JOIN reckoner.v1_history h USING(tenant_id,transaction_id)
  JOIN reckoner.transactions q ON q.tenant_id=query_tenant AND q.transaction_id=query_transaction
  LEFT JOIN reckoner.v1_merchant_identities qm ON qm.tenant_id=query_tenant
    AND qm.merchant_id=q.document->>'merchant_id'
  LEFT JOIN reckoner.v1_merchant_identities m ON m.tenant_id=h.tenant_id AND m.merchant_id=h.merchant_id
  WHERE h.occurred_at < (q.document->>'occurred_at')::timestamptz
    AND r.resolved_at < (q.document->>'occurred_at')::timestamptz
    AND r.resolved_at >= (q.document->>'occurred_at')::timestamptz - interval '90 days'
    AND r.resolution_policy_version='simulated-seven-days-v1'
    AND ((qm.identity IS NOT NULL AND m.identity=qm.identity)
      OR (h.tenant_id=query_tenant AND h.merchant_id=q.document->>'merchant_id'))
  ORDER BY r.tenant_id,r.transaction_id
$$;
CREATE FUNCTION reckoner.v1_vector_search(query_tenant text, query_transaction text,
                                        query_features public.vector, query_scaler text)
RETURNS TABLE(document jsonb,distance double precision) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog AS $$
  SELECT r.document, v.features OPERATOR(public.<->) query_features AS distance
  FROM reckoner.v1_vectors v JOIN oracle.v1_resolutions r USING(tenant_id,transaction_id)
  JOIN reckoner.v1_history h USING(tenant_id,transaction_id)
  JOIN reckoner.transactions q ON q.tenant_id=query_tenant AND q.transaction_id=query_transaction
  WHERE v.tenant_id=query_tenant AND v.scaler_id=query_scaler
    AND v.feature_version='features-v1' AND r.resolution_policy_version='simulated-seven-days-v1'
    AND (h.document->>'amount_minor')::bigint > 0
    AND h.occurred_at < (q.document->>'occurred_at')::timestamptz
    AND r.resolved_at < (q.document->>'occurred_at')::timestamptz
    AND r.resolved_at >= (q.document->>'occurred_at')::timestamptz - interval '90 days'
  ORDER BY distance,v.transaction_id LIMIT 5
$$;
CREATE FUNCTION reckoner.v1_vector_coverage(query_tenant text, query_transaction text,
                                           query_scaler text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
  SELECT NOT EXISTS (SELECT 1 FROM reckoner.v1_resolved_before(query_tenant,query_transaction) r
    JOIN reckoner.v1_history h ON h.tenant_id=query_tenant
      AND h.transaction_id=r.document->>'transaction_id'
    WHERE (h.document->>'amount_minor')::bigint > 0 AND NOT EXISTS (SELECT 1 FROM reckoner.v1_vectors v
      WHERE v.tenant_id=query_tenant AND v.transaction_id=r.document->>'transaction_id'
      AND v.scaler_id=query_scaler))
$$;
CREATE VIEW reckoner.v1_runtime_coverage AS SELECT * FROM reckoner.v1_evidence_coverage;
REVOKE ALL ON reckoner.v1_vectors,reckoner.v1_merchant_identities,
  reckoner.v1_evidence_coverage FROM PUBLIC,reckoner_runner,reckoner_api,reckoner_evaluator;
REVOKE ALL ON FUNCTION reckoner.v1_neighbours(text,text),
 reckoner.v1_merchant_resolved(text,text),reckoner.v1_vector_search(text,text,public.vector,text),
 reckoner.v1_vector_coverage(text,text,text) FROM PUBLIC;
GRANT SELECT ON reckoner.v1_runtime_coverage TO reckoner_runner;
GRANT EXECUTE ON FUNCTION reckoner.v1_neighbours(text,text),
 reckoner.v1_merchant_resolved(text,text),reckoner.v1_vector_search(text,text,public.vector,text),
 reckoner.v1_vector_coverage(text,text,text) TO reckoner_runner;

-- Foundation-supported purchases only; all other source histories remain retained.
CREATE FUNCTION reckoner.v1_comparable_candidates(query_tenant text, query_transaction text)
RETURNS TABLE(document jsonb) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
  SELECT r.document FROM reckoner.v1_resolved_before(query_tenant,query_transaction) r
  JOIN reckoner.v1_history h ON h.tenant_id=query_tenant
    AND h.transaction_id=r.document->>'transaction_id'
  WHERE (h.document->>'amount_minor')::bigint > 0
  ORDER BY r.document->>'transaction_id'
$$;
CREATE FUNCTION reckoner.v1_comparable_exclusions(query_tenant text, query_transaction text)
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
  SELECT count(*) FROM reckoner.v1_resolved_before(query_tenant,query_transaction) r
  JOIN reckoner.v1_history h ON h.tenant_id=query_tenant
    AND h.transaction_id=r.document->>'transaction_id'
  WHERE (h.document->>'amount_minor')::bigint <= 0
$$;
REVOKE ALL ON FUNCTION reckoner.v1_comparable_candidates(text,text),
  reckoner.v1_comparable_exclusions(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reckoner.v1_comparable_candidates(text,text),
  reckoner.v1_comparable_exclusions(text,text) TO reckoner_runner;
