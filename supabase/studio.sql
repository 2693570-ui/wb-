-- Vi-Smart Studio — эталоны, проекты, сессии входа
-- SQL Editor → Run (если миграция через MCP не накатилась)

create table if not exists public.studio_sessions (
  token       text primary key,
  login       text not null,
  name        text not null,
  role        text not null,
  expires_at  timestamptz not null,
  created_at  timestamptz default now()
);

create table if not exists public.studio_references (
  id          bigserial primary key,
  nm_id       bigint,
  marketplace text default 'wb',
  title       text,
  url         text,
  ctr         numeric,
  cr          numeric,
  why_works   text,
  notes       text,
  created_by  text,
  created_at  timestamptz default now()
);

create table if not exists public.studio_projects (
  id          bigserial primary key,
  title       text not null,
  keyword     text,
  strengths   text,
  status      text default 'draft',
  winner_json jsonb,
  created_by  text,
  created_at  timestamptz default now()
);
