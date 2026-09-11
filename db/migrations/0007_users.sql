-- Multi-user: one row per person using the agent. Identified by their control channel (telegram chat, whatsapp jid)
-- and, for Composio events, by the composio user id (= our uuid for users onboarded through the agent).
create table app_user (
  id uuid primary key default gen_random_uuid(),
  control_source text not null,          -- telegram | whatsapp
  control_thread_key text not null,      -- telegram chat id / whatsapp jid
  composio_user_id text unique,          -- entity id in Composio; our uuid for new users
  display_name text,
  profile text not null default '',
  language text not null default 'en',
  timezone text not null default 'UTC',
  state jsonb not null default '{}',     -- onboarding flags, pending connection requests
  created_at timestamptz not null default now(),
  unique (control_source, control_thread_key)
);
