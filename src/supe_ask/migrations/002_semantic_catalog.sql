create table if not exists ask_semantic_packs (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  source_path text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id)
);

create table if not exists ask_semantic_pack_versions (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  semantic_pack_id uuid not null references ask_semantic_packs(id) on delete cascade,
  refresh_id uuid not null,
  source_path text not null,
  status text not null,
  cluster_count int not null default 0,
  canonical_question_count int not null default 0,
  variant_count int not null default 0,
  entity_count int not null default 0,
  metric_count int not null default 0,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ask_semantic_pack_versions_lookup on ask_semantic_pack_versions(tenant_id, created_at desc);

create table if not exists ask_question_clusters (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  cluster_key text not null,
  cluster_number int not null,
  title text not null,
  description text not null,
  question_count int not null default 0,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, cluster_key)
);

create index if not exists idx_ask_question_clusters_lookup on ask_question_clusters(tenant_id, cluster_key);

create table if not exists ask_canonical_questions (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  cluster_key text not null,
  question_number int not null,
  canonical_question text not null,
  data_sources jsonb not null default '[]'::jsonb,
  complexity text not null,
  primary_entity text not null,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, question_number)
);

create index if not exists idx_ask_canonical_questions_lookup on ask_canonical_questions(tenant_id, question_number);
create index if not exists idx_ask_canonical_questions_cluster on ask_canonical_questions(tenant_id, cluster_key);

create table if not exists ask_question_variants (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  canonical_question_id uuid not null references ask_canonical_questions(id) on delete cascade,
  canonical_question_number int not null,
  variant_text text not null,
  ordinal_position int not null default 0,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ask_question_variants_lookup on ask_question_variants(tenant_id, canonical_question_number);

create table if not exists ask_entities (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  entity_key text not null,
  display_name text not null,
  aliases jsonb not null default '[]'::jsonb,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, entity_key)
);

create index if not exists idx_ask_entities_lookup on ask_entities(tenant_id, entity_key);

create table if not exists ask_metrics (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  metric_key text not null,
  display_name text not null,
  aliases jsonb not null default '[]'::jsonb,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, metric_key)
);

create index if not exists idx_ask_metrics_lookup on ask_metrics(tenant_id, metric_key);

create table if not exists ask_metric_aliases (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  metric_key text not null,
  alias text not null,
  weight int not null default 1,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ask_metric_aliases_lookup on ask_metric_aliases(tenant_id, metric_key);
create index if not exists idx_ask_metric_aliases_alias on ask_metric_aliases(tenant_id, alias);

create table if not exists ask_join_policies (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  policy_key text not null,
  from_table text not null,
  to_table text not null,
  via_tables jsonb not null default '[]'::jsonb,
  join_edges jsonb not null default '[]'::jsonb,
  preferred boolean not null default true,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, policy_key)
);

create index if not exists idx_ask_join_policies_lookup on ask_join_policies(tenant_id, from_table, to_table);

create table if not exists ask_date_policies (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  policy_key text not null,
  metric_key text,
  date_column text not null,
  time_grains jsonb not null default '[]'::jsonb,
  timezone text not null default 'Asia/Kolkata',
  semantics text not null default 'wall_clock',
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, policy_key)
);

create index if not exists idx_ask_date_policies_lookup on ask_date_policies(tenant_id, policy_key);

create table if not exists ask_threshold_policies (
  id uuid primary key,
  tenant_id bigint not null references tenants(id),
  policy_key text not null,
  metric_key text,
  threshold_name text not null,
  comparator text not null,
  threshold_value text,
  search_text text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, policy_key)
);

create index if not exists idx_ask_threshold_policies_lookup on ask_threshold_policies(tenant_id, policy_key);
