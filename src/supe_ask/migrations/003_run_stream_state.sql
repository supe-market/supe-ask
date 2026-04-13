alter table ask_runs
add column if not exists stream_state jsonb not null default '{}'::jsonb;
