-- Additive protected provider records. All workload inputs are simulated.
CREATE TABLE reckoner.v1_protocols (
  tenant_id text NOT NULL,
  protocol_id text PRIMARY KEY,
  provider text NOT NULL CHECK (provider IN ('typesafe','anthropic')),
  run_id text NOT NULL,
  maximum_cost numeric NOT NULL CHECK (maximum_cost >= 0),
  document jsonb NOT NULL,
  UNIQUE (tenant_id,protocol_id,provider,run_id),
  UNIQUE (tenant_id,protocol_id),
  CHECK (document->>'tenant_id'=tenant_id AND document->>'protocol_id'=protocol_id
    AND document->>'provider'=provider AND document->>'run_id'=run_id),
  FOREIGN KEY (tenant_id,run_id) REFERENCES reckoner.v1_runs(tenant_id,run_id)
);
CREATE TABLE reckoner.v1_protocol_closures (
  tenant_id text NOT NULL,
  protocol_id text PRIMARY KEY,
  closed_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id,protocol_id) REFERENCES reckoner.v1_protocols(tenant_id,protocol_id)
);
CREATE TABLE reckoner.v1_provider_calls (
  tenant_id text NOT NULL,
  call_id text PRIMARY KEY,
  protocol_id text NOT NULL,
  provider text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  purpose text NOT NULL,
  maximum_cost numeric NOT NULL CHECK (maximum_cost >= 0),
  document jsonb NOT NULL,
  dispatched_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,call_id),
  CHECK (document->>'tenant_id'=tenant_id AND document->>'call_id'=call_id
    AND document->>'run_id'=run_id AND document->>'task_id'=task_id
    AND document->>'protocol_id'=protocol_id AND document->>'provider'=provider),
  FOREIGN KEY (tenant_id,protocol_id,provider,run_id)
    REFERENCES reckoner.v1_protocols(tenant_id,protocol_id,provider,run_id),
  FOREIGN KEY (tenant_id,run_id,task_id) REFERENCES reckoner.v1_tasks(tenant_id,run_id,task_id)
);
CREATE TABLE reckoner.v1_settlements (
  tenant_id text NOT NULL,
  call_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('uncertain','settled')),
  usage jsonb,
  cost numeric CHECK (cost >= 0),
  settled_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (call_id,status),
  FOREIGN KEY (tenant_id,call_id) REFERENCES reckoner.v1_provider_calls(tenant_id,call_id),
  CHECK ((status='uncertain' AND cost IS NULL AND usage IS NULL) OR
         (status='settled' AND cost IS NOT NULL AND usage IS NOT NULL))
);
CREATE TABLE reckoner.v1_provider_responses (
  tenant_id text NOT NULL,
  call_id text PRIMARY KEY,
  category text NOT NULL,
  body jsonb,
  score jsonb NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  CHECK (score->>'tenant_id'=tenant_id AND score->>'call_id'=call_id),
  FOREIGN KEY (tenant_id,call_id) REFERENCES reckoner.v1_provider_calls(tenant_id,call_id)
);
CREATE TABLE reckoner.v1_provider_state (
  tenant_id text NOT NULL DEFAULT '_provider-wide' CHECK (tenant_id='_provider-wide'),
  provider text PRIMARY KEY CHECK (provider IN ('typesafe','anthropic')),
  consecutive_failures integer NOT NULL DEFAULT 0,
  open_until timestamptz,
  active_call text REFERENCES reckoner.v1_provider_calls(call_id),
  next_dispatch_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE reckoner.v1_legacy_provenance (
  tenant_id text NOT NULL,
  provenance_id text PRIMARY KEY,
  document jsonb NOT NULL,
  verified_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION reckoner.v1_provider_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'provider accounting records are immutable' USING ERRCODE='check_violation';
END $$;
DO $$ DECLARE name text; BEGIN
  FOREACH name IN ARRAY ARRAY['v1_protocols','v1_protocol_closures','v1_provider_calls',
    'v1_settlements','v1_provider_responses','v1_legacy_provenance'] LOOP
    EXECUTE format('CREATE TRIGGER provider_immutable BEFORE UPDATE OR DELETE ON reckoner.%I '
      'FOR EACH ROW EXECUTE FUNCTION reckoner.v1_provider_immutable()',name);
  END LOOP;
END $$;
GRANT SELECT ON reckoner.v1_protocols, reckoner.v1_protocol_closures,
  reckoner.v1_provider_calls, reckoner.v1_settlements, reckoner.v1_provider_responses,
  reckoner.v1_provider_state, reckoner.v1_legacy_provenance TO reckoner_runner;
GRANT INSERT ON reckoner.v1_protocols, reckoner.v1_protocol_closures,
  reckoner.v1_provider_calls, reckoner.v1_settlements, reckoner.v1_provider_responses,
  reckoner.v1_provider_state TO reckoner_runner;
GRANT UPDATE ON reckoner.v1_provider_state TO reckoner_runner;
