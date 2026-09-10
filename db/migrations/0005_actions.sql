-- action table exists since 0001. Phase 4 adds the tiny chat_state table for "edit mode" per thread.
create table chat_state (
  user_id uuid not null,
  thread_key text not null,
  pending_edit_action_id uuid references action(id),
  updated_at timestamptz not null default now(),
  primary key (user_id, thread_key)
);
create index on action (user_id, status);
