-- Declared merchant scope is independent of imported histories, including empty tenants.
CREATE FUNCTION reckoner.v1_shared_merchant_scope(query_tenant text, query_transaction text)
RETURNS TABLE(tenant_id text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog AS $$
  SELECT DISTINCT m.tenant_id FROM reckoner.transactions q
  JOIN reckoner.v1_merchant_identities qm ON qm.tenant_id=q.tenant_id
    AND qm.merchant_id=q.document->>'merchant_id'
  JOIN reckoner.v1_merchant_identities m ON m.identity=qm.identity
  WHERE q.tenant_id=query_tenant AND q.transaction_id=query_transaction
  ORDER BY m.tenant_id
$$;
REVOKE ALL ON FUNCTION reckoner.v1_shared_merchant_scope(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reckoner.v1_shared_merchant_scope(text,text) TO reckoner_runner;
