-- External, SHA-bound paid-run approvals. All workload inputs are simulated.
-- Approval records are supplied from outside the immutable protocol body; the
-- owner records them together with the protocol's full reserved envelope.
CREATE TABLE reckoner.v1_ledger_identity (
  tenant_id text NOT NULL DEFAULT '_provider-wide' CHECK (tenant_id='_provider-wide'),
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  ledger_uuid uuid NOT NULL DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO reckoner.v1_ledger_identity DEFAULT VALUES;
CREATE TABLE reckoner.v1_protocol_approvals (
  tenant_id text NOT NULL DEFAULT '_provider-wide' CHECK (tenant_id='_provider-wide'),
  approval_id text PRIMARY KEY CHECK (length(approval_id) BETWEEN 1 AND 256),
  protocol_sha256 text NOT NULL UNIQUE CHECK (protocol_sha256 ~ '^[a-f0-9]{64}$'),
  document jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  CHECK (document->>'approval_id'=approval_id AND document->>'protocol_sha256'=protocol_sha256),
  CHECK (NOT document ? 'approved')
);
CREATE TABLE reckoner.v1_experiment_protocols (
  tenant_id text NOT NULL DEFAULT '_provider-wide' CHECK (tenant_id='_provider-wide'),
  protocol_sha256 text PRIMARY KEY
    REFERENCES reckoner.v1_protocol_approvals(protocol_sha256),
  document jsonb NOT NULL,
  CHECK (document->>'protocol_sha256'=protocol_sha256),
  CHECK (NOT document ? 'approved')
);
CREATE TABLE reckoner.v1_protocol_authorizations (
  tenant_id text NOT NULL,
  protocol_id text PRIMARY KEY,
  protocol_sha256 text NOT NULL REFERENCES reckoner.v1_experiment_protocols(protocol_sha256),
  approval_id text NOT NULL REFERENCES reckoner.v1_protocol_approvals(approval_id),
  FOREIGN KEY (tenant_id,protocol_id) REFERENCES reckoner.v1_protocols(tenant_id,protocol_id)
);
DO $$ DECLARE name text; BEGIN
  FOREACH name IN ARRAY ARRAY['v1_ledger_identity','v1_protocol_approvals',
    'v1_experiment_protocols','v1_protocol_authorizations'] LOOP
    EXECUTE format('CREATE TRIGGER provider_immutable BEFORE UPDATE OR DELETE ON reckoner.%I '
      'FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable()',name);
  END LOOP;
END $$;
-- Envelopes are created only by the owner when an approval is recorded.
REVOKE INSERT ON reckoner.v1_protocols FROM reckoner_runner;
GRANT SELECT ON reckoner.v1_ledger_identity, reckoner.v1_protocol_approvals,
  reckoner.v1_experiment_protocols, reckoner.v1_protocol_authorizations TO reckoner_runner;
