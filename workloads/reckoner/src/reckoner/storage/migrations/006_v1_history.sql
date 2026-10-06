-- Simulated source-backed working sets; source archive retains complete histories.
CREATE TABLE reckoner.v1_history (
  tenant_id text NOT NULL,
  transaction_id text NOT NULL,
  occurred_at timestamptz NOT NULL,
  account_id text NOT NULL,
  card_id text NOT NULL,
  merchant_id text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, transaction_id),
  FOREIGN KEY (tenant_id, transaction_id)
    REFERENCES reckoner.transactions (tenant_id, transaction_id),
  CHECK (document->>'tenant_id'=tenant_id AND document->>'transaction_id'=transaction_id
    AND (document->>'occurred_at')::timestamptz=occurred_at
    AND document->>'account_id'=account_id AND document->>'card_id'=card_id
    AND document->>'merchant_id'=merchant_id)
);
CREATE INDEX v1_history_time ON reckoner.v1_history (tenant_id, occurred_at);
CREATE INDEX v1_history_card_time ON reckoner.v1_history (tenant_id, card_id, occurred_at);

CREATE TABLE oracle.v1_resolutions (
  tenant_id text NOT NULL,
  transaction_id text NOT NULL,
  resolution_policy_version text NOT NULL,
  resolved_at timestamptz NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, transaction_id, resolution_policy_version),
  FOREIGN KEY (tenant_id, transaction_id)
    REFERENCES reckoner.v1_history (tenant_id, transaction_id),
  CHECK (document->>'tenant_id'=tenant_id AND document->>'transaction_id'=transaction_id
    AND document->>'resolution_policy_version'=resolution_policy_version
    AND (document->>'resolved_at')::timestamptz=resolved_at)
);
CREATE INDEX v1_resolution_time ON oracle.v1_resolutions (tenant_id, resolved_at);

CREATE TRIGGER preserve_history BEFORE UPDATE ON reckoner.v1_history
  FOR EACH ROW EXECUTE FUNCTION reckoner.v1_preserve_document();
CREATE TRIGGER preserve_resolution BEFORE UPDATE ON oracle.v1_resolutions
  FOR EACH ROW EXECUTE FUNCTION reckoner.v1_preserve_document();

-- Callers supply a canonical query transaction, never an arbitrary future cutoff.
CREATE FUNCTION reckoner.v1_history_before(query_tenant text, query_transaction text)
RETURNS TABLE(document jsonb) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog AS $$
  SELECT h.document FROM reckoner.v1_history h
  JOIN reckoner.transactions q ON q.tenant_id=query_tenant
    AND q.transaction_id=query_transaction
  WHERE h.tenant_id=query_tenant
    AND h.occurred_at < (q.document->>'occurred_at')::timestamptz
    AND h.occurred_at >= (q.document->>'occurred_at')::timestamptz - interval '30 days'
  ORDER BY h.occurred_at,h.transaction_id
$$;
CREATE FUNCTION reckoner.v1_resolved_before(query_tenant text, query_transaction text)
RETURNS TABLE(document jsonb) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog AS $$
  SELECT r.document FROM oracle.v1_resolutions r
  JOIN reckoner.v1_history h ON h.tenant_id=r.tenant_id AND h.transaction_id=r.transaction_id
  JOIN reckoner.transactions q ON q.tenant_id=query_tenant
    AND q.transaction_id=query_transaction
  WHERE r.tenant_id=query_tenant
    AND r.resolution_policy_version='simulated-seven-days-v1'
    AND h.occurred_at < (q.document->>'occurred_at')::timestamptz
    AND r.resolved_at < (q.document->>'occurred_at')::timestamptz
    AND r.resolved_at >= (q.document->>'occurred_at')::timestamptz - interval '90 days'
  ORDER BY r.resolved_at,r.transaction_id
$$;
-- Prior-card state remains available when the current evidence window is empty.
CREATE FUNCTION reckoner.v1_previous_card(query_tenant text, query_transaction text)
RETURNS TABLE(document jsonb) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog AS $$
  SELECT h.document FROM reckoner.v1_history h
  JOIN reckoner.transactions q ON q.tenant_id=query_tenant
    AND q.transaction_id=query_transaction
  WHERE h.tenant_id=query_tenant AND h.card_id=q.document->>'card_id'
    AND h.occurred_at < (q.document->>'occurred_at')::timestamptz
  ORDER BY h.occurred_at DESC,h.transaction_id LIMIT 1
$$;
REVOKE ALL ON reckoner.v1_history, oracle.v1_resolutions FROM PUBLIC,
  reckoner_runner, reckoner_evaluator, reckoner_api;
REVOKE ALL ON FUNCTION reckoner.v1_history_before(text,text),
  reckoner.v1_resolved_before(text,text), reckoner.v1_previous_card(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reckoner.v1_history_before(text,text),
  reckoner.v1_resolved_before(text,text), reckoner.v1_previous_card(text,text) TO reckoner_runner;
