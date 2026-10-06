-- Additive records for the simulated Reckoner v1 workflow. Baseline tables remain intact.
CREATE TABLE reckoner.v1_configs (
  tenant_id text NOT NULL,
  config_id text NOT NULL,
  threshold_config_id text NOT NULL,
  workflow_version text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, config_id),
  FOREIGN KEY (tenant_id, threshold_config_id)
    REFERENCES reckoner.threshold_configs (tenant_id, config_id),
  CHECK (document->>'tenant_id' = tenant_id AND document->>'config_id' = config_id)
);

CREATE TABLE reckoner.v1_runs (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  config_id text NOT NULL,
  experiment_id text NOT NULL,
  purpose text NOT NULL CHECK (purpose IN
    ('pilot','calibration','development','validation','graph-comparison','final','judge','fabricated')),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','complete','incomplete','blocked')),
  document jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, run_id),
  UNIQUE (tenant_id, run_id, config_id),
  FOREIGN KEY (tenant_id, config_id) REFERENCES reckoner.v1_configs (tenant_id, config_id),
  CHECK (document->>'tenant_id' = tenant_id AND document->>'run_id' = run_id
    AND document->>'config_id' = config_id AND document->>'experiment_id' = experiment_id)
);

CREATE TABLE reckoner.v1_tasks (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  transaction_id text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','completed','failed','uncertain')),
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, run_id, task_id),
  UNIQUE (tenant_id, run_id, transaction_id),
  UNIQUE (tenant_id, run_id, task_id, transaction_id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES reckoner.v1_runs (tenant_id, run_id),
  FOREIGN KEY (tenant_id, transaction_id)
    REFERENCES reckoner.transactions (tenant_id, transaction_id),
  CHECK (document->>'tenant_id' = tenant_id AND document->>'run_id' = run_id
    AND document->>'task_id' = task_id AND document->>'transaction_id' = transaction_id)
);

CREATE TABLE reckoner.v1_evidence (
  tenant_id text NOT NULL,
  evidence_id text NOT NULL,
  transaction_id text NOT NULL,
  query_time timestamptz NOT NULL,
  coverage_status text NOT NULL CHECK (coverage_status IN ('available','partial','unavailable')),
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, evidence_id),
  UNIQUE (tenant_id, evidence_id, transaction_id),
  FOREIGN KEY (tenant_id, transaction_id)
    REFERENCES reckoner.transactions (tenant_id, transaction_id),
  CHECK (document->>'tenant_id' = tenant_id AND document->>'evidence_id' = evidence_id
    AND document->>'transaction_id' = transaction_id)
);
CREATE INDEX v1_evidence_query_idx ON reckoner.v1_evidence (tenant_id, transaction_id, query_time);

CREATE TABLE reckoner.v1_decisions (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  transaction_id text NOT NULL,
  decision_id text NOT NULL,
  config_id text NOT NULL,
  evidence_id text NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('auto-approve','auto-decline','escalate')),
  scorer_status text NOT NULL CHECK (scorer_status IN ('succeeded','failed','uncertain','unavailable')),
  call_id text,
  document jsonb NOT NULL,
  completed_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, decision_id),
  UNIQUE (tenant_id, decision_id, evidence_id),
  UNIQUE (tenant_id, run_id, task_id),
  FOREIGN KEY (tenant_id, run_id, task_id, transaction_id)
    REFERENCES reckoner.v1_tasks (tenant_id, run_id, task_id, transaction_id),
  FOREIGN KEY (tenant_id, run_id, config_id)
    REFERENCES reckoner.v1_runs (tenant_id, run_id, config_id),
  FOREIGN KEY (tenant_id, evidence_id, transaction_id)
    REFERENCES reckoner.v1_evidence (tenant_id, evidence_id, transaction_id),
  CHECK (document->>'tenant_id' = tenant_id AND document->>'decision_id' = decision_id
    AND document->>'run_id' = run_id AND document->>'task_id' = task_id
    AND document->>'transaction_id' = transaction_id AND document->>'config_id' = config_id
    AND document->>'evidence_id' = evidence_id),
  CHECK (scorer_status <> 'succeeded' OR call_id IS NOT NULL)
);

CREATE TABLE reckoner.v1_cases (
  tenant_id text NOT NULL,
  case_id text NOT NULL,
  decision_id text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','reviewed')),
  version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  document jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, case_id),
  UNIQUE (tenant_id, decision_id),
  UNIQUE (tenant_id, case_id, decision_id),
  FOREIGN KEY (tenant_id, decision_id) REFERENCES reckoner.v1_decisions (tenant_id, decision_id),
  CHECK (document->>'tenant_id' = tenant_id AND document->>'case_id' = case_id
    AND document->>'decision_id' = decision_id)
);
CREATE INDEX v1_cases_queue_idx ON reckoner.v1_cases (tenant_id, status, created_at, case_id);

CREATE TABLE reckoner.v1_notes (
  tenant_id text NOT NULL,
  note_id text NOT NULL,
  case_id text NOT NULL,
  decision_id text NOT NULL,
  evidence_id text NOT NULL,
  generation_status text NOT NULL CHECK (generation_status IN ('succeeded','failed','degraded')),
  prompt_version text NOT NULL,
  document jsonb NOT NULL,
  completed_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, note_id),
  FOREIGN KEY (tenant_id, case_id, decision_id)
    REFERENCES reckoner.v1_cases (tenant_id, case_id, decision_id),
  FOREIGN KEY (tenant_id, decision_id, evidence_id)
    REFERENCES reckoner.v1_decisions (tenant_id, decision_id, evidence_id),
  CHECK (document->>'tenant_id' = tenant_id AND document->>'note_id' = note_id
    AND document->>'case_id' = case_id AND document->>'decision_id' = decision_id
    AND document->>'evidence_id' = evidence_id)
);

CREATE TABLE reckoner.v1_reviews (
  tenant_id text NOT NULL,
  action_id text NOT NULL,
  case_id text NOT NULL,
  decision_id text NOT NULL,
  idempotency_key text NOT NULL,
  prior_case_version integer NOT NULL CHECK (prior_case_version >= 1),
  reviewer_type text NOT NULL CHECK (reviewer_type IN ('human','simulated')),
  verdict text NOT NULL CHECK (verdict IN ('approve','decline')),
  document jsonb NOT NULL,
  reviewed_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, action_id),
  UNIQUE (tenant_id, case_id, idempotency_key),
  UNIQUE (tenant_id, case_id, prior_case_version),
  FOREIGN KEY (tenant_id, case_id, decision_id)
    REFERENCES reckoner.v1_cases (tenant_id, case_id, decision_id),
  CHECK (document->>'tenant_id' = tenant_id AND document->>'action_id' = action_id
    AND document->>'case_id' = case_id AND document->>'decision_id' = decision_id)
);

CREATE TABLE reckoner.v1_outbox (
  tenant_id text NOT NULL,
  event_id text NOT NULL,
  run_id text NOT NULL,
  task_id text,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','exported','failed')),
  document jsonb NOT NULL,
  payload bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, event_id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES reckoner.v1_runs (tenant_id, run_id),
  FOREIGN KEY (tenant_id, run_id, task_id)
    REFERENCES reckoner.v1_tasks (tenant_id, run_id, task_id),
  CHECK (document->>'tenant_id' = tenant_id AND document->>'event_id' = event_id)
);

CREATE FUNCTION reckoner.v1_preserve_document() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (to_jsonb(NEW) - 'status' - 'version') IS DISTINCT FROM
     (to_jsonb(OLD) - 'status' - 'version') THEN
    RAISE EXCEPTION 'v1 documents and indexed identities are immutable' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['v1_configs','v1_runs','v1_tasks','v1_evidence',
    'v1_decisions','v1_cases','v1_notes','v1_reviews','v1_outbox']
  LOOP
    EXECUTE format('CREATE TRIGGER preserve_document BEFORE UPDATE ON reckoner.%I '
      'FOR EACH ROW EXECUTE FUNCTION reckoner.v1_preserve_document()', table_name);
  END LOOP;
END;
$$;
REVOKE ALL ON FUNCTION reckoner.v1_preserve_document() FROM PUBLIC;

CREATE VIEW reckoner.api_v1_runs AS
SELECT tenant_id, run_id, config_id, experiment_id, purpose, status, created_at FROM reckoner.v1_runs;
CREATE VIEW reckoner.api_v1_tasks AS
SELECT tenant_id, run_id, task_id, transaction_id, status FROM reckoner.v1_tasks;
CREATE VIEW reckoner.api_v1_decisions AS
SELECT tenant_id, run_id, task_id, transaction_id, decision_id, outcome, scorer_status, completed_at
FROM reckoner.v1_decisions;
CREATE VIEW reckoner.api_v1_cases AS
SELECT tenant_id, case_id, decision_id, status, version, created_at FROM reckoner.v1_cases;
CREATE VIEW reckoner.api_v1_notes AS
SELECT tenant_id, note_id, case_id, decision_id, generation_status, document, completed_at
FROM reckoner.v1_notes;
CREATE VIEW reckoner.api_v1_reviews AS
SELECT tenant_id, action_id, case_id, reviewer_type, verdict, reviewed_at FROM reckoner.v1_reviews;

REVOKE ALL ON reckoner.v1_configs, reckoner.v1_runs, reckoner.v1_tasks,
  reckoner.v1_evidence, reckoner.v1_decisions, reckoner.v1_cases, reckoner.v1_notes,
  reckoner.v1_reviews, reckoner.v1_outbox, reckoner.api_v1_runs, reckoner.api_v1_tasks,
  reckoner.api_v1_decisions, reckoner.api_v1_cases, reckoner.api_v1_notes,
  reckoner.api_v1_reviews FROM PUBLIC;
GRANT SELECT ON reckoner.v1_configs, reckoner.v1_runs, reckoner.v1_tasks,
  reckoner.v1_evidence, reckoner.v1_decisions, reckoner.v1_cases, reckoner.v1_notes,
  reckoner.v1_reviews, reckoner.v1_outbox TO reckoner_runner, reckoner_evaluator;
GRANT INSERT ON reckoner.v1_evidence, reckoner.v1_decisions, reckoner.v1_cases,
  reckoner.v1_notes, reckoner.v1_outbox TO reckoner_runner;
GRANT UPDATE (status) ON reckoner.v1_tasks, reckoner.v1_outbox TO reckoner_runner;
GRANT SELECT ON reckoner.api_v1_runs, reckoner.api_v1_tasks, reckoner.api_v1_decisions,
  reckoner.api_v1_cases, reckoner.api_v1_notes, reckoner.api_v1_reviews TO reckoner_api;
