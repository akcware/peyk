create table triage (
  observation_id uuid primary key references observation(id),
  urgency smallint not null check (urgency between 1 and 5),
  category text not null,          -- person | transactional | newsletter | automated | calendar | other
  reason text not null,
  model_id text not null,
  latency_ms int,
  created_at timestamptz default now()
);

create table sent_notification (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  observation_id uuid references observation(id),
  thread_key text,
  urgency smallint,
  sent_at timestamptz not null default now(),
  tg_message_id bigint,
  user_feedback text                 -- null | useful | noise  (Telegram inline button)
);
create index on sent_notification (user_id, sent_at);

create table mute_rule (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  kind text not null,                -- thread | sender | category
  value text not null,
  until timestamptz,                 -- null = forever
  unique (user_id, kind, value)
);

create table budget_settings (
  user_id uuid primary key,
  daily_quota int not null default 5,
  thread_cooldown_minutes int not null default 240,
  quiet_hours int4range,             -- e.g. [23,8) wraps midnight
  bypass_urgency smallint not null default 5
);
