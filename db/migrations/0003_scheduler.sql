create table scheduled_job (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  run_at timestamptz not null,
  kind text not null,                -- morning_brief | followup | recheck_thread | reconcile | event_reminder
  payload jsonb not null default '{}',
  recurrence text,                   -- null | 'daily@08:00' | 'every:10m'  (simple format, not cron)
  status text not null default 'pending',  -- pending | claimed | done | failed | cancelled
  created_by text not null,          -- user | agent | system
  claimed_at timestamptz,
  created_at timestamptz default now()
);
create index on scheduled_job (user_id, status, run_at) where status = 'pending';
