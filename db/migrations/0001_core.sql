create extension if not exists pgcrypto;

create table observation (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null,
  source        text not null,          -- gmail | telegram | calendar | whatsapp | system
  source_key    text not null,          -- source's natural id (gmail message id, tg update id, ...)
  kind          text not null,          -- message_in | message_out | event_starting | tick
  occurred_at   timestamptz not null,
  received_at   timestamptz not null default now(),
  thread_key    text,
  payload       jsonb not null,
  is_backfill   boolean not null default false,
  status        text not null default 'new',   -- new | claimed | done | failed
  claimed_at    timestamptz,
  attempts      int not null default 0,
  unique (user_id, source, source_key)
);
create index on observation (user_id, status, received_at) where status = 'new';
create index on observation (user_id, thread_key);

create table person (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  display_name text
);

create table identity (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  person_id uuid not null references person(id),
  kind text not null,                  -- email | phone | tg_user_id | slack_uid
  value text not null,                 -- normalized
  confidence real not null default 1.0,
  unique (user_id, kind, value)
);

create table action (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  channel text not null,               -- gmail | telegram | ...
  thread_key text,
  content jsonb not null,
  content_hash text not null,
  status text not null default 'draft', -- draft | awaiting_approval | approved | sent | failed | rejected
  approved_hash text,
  external_id text,                    -- source id after send
  created_at timestamptz not null default now(),
  sent_at timestamptz
);

create table source_cursor (
  user_id uuid not null,
  source text not null,
  cursor text not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, source)
);
