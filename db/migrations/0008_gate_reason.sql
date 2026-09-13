-- Why the gate let an observation through or held it back (workers/gate.py Reason). Stored, not only logged, so the
-- chat can say "I saw it, today's budget was used up" and an investigation does not depend on log retention.
alter table triage add column if not exists gate_reason text;
