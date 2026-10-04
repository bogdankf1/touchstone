-- Provider evidence behind an owner reconciliation of an uncertain or unanswered call.
-- Recorded with the settlement; immutable. All workload data is simulated.
CREATE TABLE reckoner.v1_settlement_evidence (
  tenant_id text NOT NULL,
  call_id text PRIMARY KEY,
  document jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  CHECK (document->>'call_id'=call_id AND document->>'document_sha256' ~ '^[a-f0-9]{64}$'),
  FOREIGN KEY (tenant_id,call_id) REFERENCES reckoner.v1_provider_calls(tenant_id,call_id)
);
CREATE TRIGGER provider_immutable BEFORE UPDATE OR DELETE ON reckoner.v1_settlement_evidence
  FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable();
GRANT SELECT ON reckoner.v1_settlement_evidence TO reckoner_runner;
