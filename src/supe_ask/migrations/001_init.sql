create table if not exists ask_threads (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  title text not null,
  created_by text not null,
  archived boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ask_threads_tenant on ask_threads(tenant_id, updated_at desc);

create table if not exists ask_messages (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  thread_id uuid not null references ask_threads(id) on delete cascade,
  role text not null,
  content text not null,
  run_id uuid null,
  created_at timestamptz not null default now()
);

create index if not exists idx_ask_messages_thread on ask_messages(thread_id, created_at asc);

create table if not exists ask_runs (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  thread_id uuid not null references ask_threads(id) on delete cascade,
  message_id uuid not null references ask_messages(id) on delete cascade,
  question text not null,
  status text not null,
  title text,
  assistant_summary text,
  python_code text,
  retrieval_context jsonb,
  artifact_plan jsonb,
  error_message text,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ask_runs_thread on ask_runs(thread_id, created_at asc);
create index if not exists idx_ask_runs_status on ask_runs(tenant_id, status, created_at desc);

create table if not exists ask_run_events (
  id bigserial primary key,
  tenant_id bigint not null references tenants(id),
  run_id uuid not null references ask_runs(id) on delete cascade,
  event_type text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_ask_run_events_run on ask_run_events(run_id, id asc);

create table if not exists ask_run_artifacts (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  run_id uuid not null references ask_runs(id) on delete cascade,
  artifact_type text not null,
  title text not null,
  payload jsonb not null,
  storage_backend text not null default 'database',
  object_key text,
  content_type text,
  byte_size bigint,
  metadata jsonb not null default '{}'::jsonb,
  ordinal int not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists idx_ask_run_artifacts_run on ask_run_artifacts(run_id, ordinal asc);

create table if not exists ask_run_executions (
  run_id uuid primary key references ask_runs(id) on delete cascade,
  tenant_id bigint not null references tenants(id),
  backend text not null,
  status text not null,
  task_arn text,
  callback_token_hash text,
  input_object_key text,
  last_callback_sequence int not null default 0,
  last_heartbeat_at timestamptz,
  cancel_requested_at timestamptz,
  runner_started_at timestamptz,
  runner_completed_at timestamptz,
  stop_reason text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ask_run_executions_status on ask_run_executions(status, updated_at desc);
create index if not exists idx_ask_run_executions_backend on ask_run_executions(backend, status, updated_at desc);
create unique index if not exists idx_ask_run_executions_task_arn on ask_run_executions(task_arn) where task_arn is not null;

create table if not exists ask_catalog_tables (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  schema_name text not null default 'public',
  table_name text not null,
  display_name text,
  description text,
  tenant_column text,
  primary_key_columns jsonb not null default '[]'::jsonb,
  date_columns jsonb not null default '[]'::jsonb,
  metric_hints jsonb not null default '[]'::jsonb,
  dimension_hints jsonb not null default '[]'::jsonb,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, schema_name, table_name)
);

create index if not exists idx_ask_catalog_tables_lookup on ask_catalog_tables(tenant_id, table_name);

create table if not exists ask_catalog_columns (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  table_name text not null,
  column_name text not null,
  data_type text not null,
  is_nullable boolean not null default true,
  ordinal_position int not null default 0,
  is_primary_key boolean not null default false,
  is_foreign_key boolean not null default false,
  references_table text,
  references_column text,
  semantic_role text,
  description text,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, table_name, column_name)
);

create index if not exists idx_ask_catalog_columns_lookup on ask_catalog_columns(tenant_id, table_name, column_name);

create table if not exists ask_catalog_relationships (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  from_table text not null,
  from_column text not null,
  to_table text not null,
  to_column text not null,
  relationship_type text not null default 'foreign_key',
  cardinality text,
  source text not null default 'database',
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, from_table, from_column, to_table, to_column, source)
);

create index if not exists idx_ask_catalog_relationships_lookup on ask_catalog_relationships(tenant_id, from_table, to_table);

create table if not exists ask_catalog_aliases (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  object_type text not null,
  object_name text not null,
  table_name text,
  column_name text,
  alias text not null,
  weight int not null default 1,
  source text not null default 'manifest',
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ask_catalog_aliases_lookup on ask_catalog_aliases(tenant_id, object_type, object_name);
create index if not exists idx_ask_catalog_aliases_alias on ask_catalog_aliases(tenant_id, alias);

create table if not exists ask_catalog_refreshes (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  status text not null,
  strategy text not null default 'catalog_refresh',
  refreshed_tables int not null default 0,
  refreshed_columns int not null default 0,
  refreshed_relationships int not null default 0,
  refreshed_aliases int not null default 0,
  error_message text,
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ask_catalog_refreshes_lookup on ask_catalog_refreshes(tenant_id, created_at desc);
