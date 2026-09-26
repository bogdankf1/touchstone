ALTER TABLE reckoner.tasks
  DROP CONSTRAINT task_preflight_complete,
  ADD COLUMN provider_span_id text,
  ADD CONSTRAINT task_preflight_complete CHECK (
    (request_document IS NULL AND request_sha256 IS NULL
      AND input_token_estimate IS NULL AND reservation_input_tokens IS NULL
      AND reservation_cost IS NULL AND trace_id IS NULL AND span_id IS NULL
      AND provider_span_id IS NULL AND event_id IS NULL)
    OR
    (request_document IS NOT NULL AND length(request_sha256) = 64
      AND input_token_estimate IS NOT NULL AND reservation_input_tokens IS NOT NULL
      AND reservation_cost IS NOT NULL AND length(trace_id) = 32
      AND length(span_id) = 16 AND length(provider_span_id) = 16
      AND event_id IS NOT NULL)
  );

ALTER TABLE reckoner.runs
  ADD COLUMN provider_call_mode text
    CHECK (provider_call_mode IN ('fake', 'measured'));
