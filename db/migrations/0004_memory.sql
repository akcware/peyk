create extension if not exists vector;
create table memory (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  text text not null,
  embedding vector(1024),            -- Bedrock Titan Embeddings v2
  source_observation_id uuid,
  created_at timestamptz default now()
);
create index on memory using hnsw (embedding vector_cosine_ops);
