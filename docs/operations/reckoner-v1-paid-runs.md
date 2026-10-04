# Reckoner v1 paid-run protocols (operator guide)

All workload data is simulated. This guide covers the protocol commands only; the
evidence-preparation procedure lives in the Reckoner v1 runbook. Approving this
implementation does not authorize any paid call: every run needs its own external
approval record bound to the exact protocol SHA-256.

## Lifecycle

Each step takes `--env-file` with only the keys that step accepts; the root `.env` is
never read.

1. `reckoner v1 protocol ledger --provider typesafe|anthropic --output NEW.json`
   (owner): reconciled provider snapshot, ledger identity and unresolved items.
2. `reckoner v1 protocol declare-run ...` (owner): freezes the explicit or active
   configuration for a new run.
3. `reckoner v1 protocol measure ...` (runner) and `reckoner v1 protocol draft ...`, or
   `reckoner v1 protocol draft-notes ...` for notes and judges. A draft pins cases,
   evidence manifests and arms, prices, bounds, per-attempt maximum, cap, ledger,
   approver, and the expected coverage gaps from the published evidence. Its `.md` file
   is the owner-facing approval request. A draft is not execution.
4. The owner replies; the controller records that reply as a
   `reckoner-protocol-approval-v1` file quoting the owner's statement. No command
   generates approvals.
5. `reckoner v1 protocol reserve --protocol P --approval A` (owner): records the approval
   and reserves every envelope's full maximum.
6. `reckoner v1 protocol execute --protocol-sha256 S --output NEW_DIR` (runner and that
   protocol's provider key only): runs sequentially under stop-on-limit and retains raw
   outputs.
7. `reckoner v1 protocol close --protocol-sha256 S` (runner): releases unused envelope
   capacity, for example after the evaluator has run the judge stages.
8. `reckoner v1 protocol verify --protocol-sha256 S --report R` (runner): reads the
   recorded protocol and transport label.

## Recovering an unanswered or in-flight call

A call can be left without a provider response by a crash, a lost connection, or a
process killed between reservation and response. Such a call is **never-answered** (no
persisted response) or **dispatched-unknown** (a response row without a body). Its
maximum stays reserved; it is never auto-zeroed and never re-sent.

1. `reckoner v1 protocol execute --protocol-sha256 S --output NEW_DIR` (resume). The
   scorer finds the reservation without a response, records it **uncertain**, and clears
   the provider's active-dispatch marker. It does not send the request again. Cases that
   were never dispatched are dispatched once.
2. `reckoner v1 protocol close --protocol-sha256 S`, if the resume did not already close
   the envelopes.
3. Obtain the provider's own record of that request: a usage record, invoice line or
   request log. Store it as a `reckoner-settlement-evidence-v1` document.
4. `reckoner v1 protocol settle --call-id C --input-tokens N --output-tokens M --evidence
   FILE` (owner). The counts must equal the evidence and any usage the stored response
   reported, and zero is accepted only when the evidence states zero. The cost uses the
   protocol's recorded price.

If no provider evidence exists, the call stays uncertain. Its maximum keeps counting
against the provider cap, and no new protocol on that provider can be reserved.

## Residual risk at the database level

The database refuses a runner-role settlement of a call that was recorded uncertain or has
no response, and requires matching evidence for any such settlement (migrations 017–018).
One residual remains:

- A process holding runner credentials could insert a fabricated response for a reserved
  call that was never marked uncertain, then settle it as a normal responded call.
- The application closes this gap: a resume marks such calls uncertain before anything
  else.
- Operators must also keep runner credentials out of untrusted processes and must not
  write responses by hand.
