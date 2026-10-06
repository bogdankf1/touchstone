-- The transport label of a recorded protocol's execution is persisted once, so
-- verification never trusts an operator flag. All workload data is simulated.
CREATE TABLE reckoner.v1_protocol_executions (
  tenant_id text NOT NULL DEFAULT '_provider-wide' CHECK (tenant_id='_provider-wide'),
  protocol_sha256 text PRIMARY KEY
    REFERENCES reckoner.v1_experiment_protocols(protocol_sha256),
  execution_kind text NOT NULL CHECK (execution_kind IN ('fixture','measured')),
  started_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER provider_immutable BEFORE UPDATE OR DELETE ON reckoner.v1_protocol_executions
  FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable();
GRANT SELECT, INSERT ON reckoner.v1_protocol_executions TO reckoner_runner;
