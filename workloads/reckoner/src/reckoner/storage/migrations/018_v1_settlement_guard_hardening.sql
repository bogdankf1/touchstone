-- Harden the reconciliation guard: a fixed search path with pg_temp last and
-- schema-qualified catalog references (a session's temporary tables cannot shadow
-- pg_roles), both current and session roles checked, and the settlement bound to the
-- exact usage and cost of its recorded evidence. Simulated workload data only.
CREATE OR REPLACE FUNCTION reckoner.v1_settlement_guard() RETURNS trigger
  LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  evidence jsonb;
BEGIN
  IF NEW.status = 'settled' AND (
       EXISTS (SELECT 1 FROM reckoner.v1_settlements u
               WHERE u.call_id = NEW.call_id AND u.status = 'uncertain')
       OR NOT EXISTS (SELECT 1 FROM reckoner.v1_provider_responses r
                      WHERE r.call_id = NEW.call_id)) THEN
    IF (pg_catalog.pg_has_role(current_user, 'reckoner_runner', 'member')
        AND NOT (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user))
       OR (pg_catalog.pg_has_role(session_user, 'reckoner_runner', 'member')
        AND NOT (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = session_user))
    THEN
      RAISE EXCEPTION 'reconciling an uncertain or unanswered call requires the owner'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT e.document INTO evidence FROM reckoner.v1_settlement_evidence e
      WHERE e.call_id = NEW.call_id;
    IF evidence IS NULL THEN
      RAISE EXCEPTION 'reconciliation requires recorded provider evidence'
        USING ERRCODE = 'check_violation';
    END IF;
    IF evidence->'usage' IS DISTINCT FROM NEW.usage
       OR (evidence->>'cost')::numeric IS DISTINCT FROM NEW.cost THEN
      RAISE EXCEPTION 'settlement differs from its recorded provider evidence'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NULL;
END $$;
