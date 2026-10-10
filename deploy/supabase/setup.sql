-- BabyCare AI on Supabase FREE plan: run once in the SQL editor after the Render API is live.
-- Idempotent. Placeholders __API_ORIGIN__ and __SCHEDULER_KEY__ are filled by scripts/configure_deployment.py
-- into deploy/supabase/setup.generated.sql (git-ignored because it contains the scheduler key).

-- 1. Extensions available on the free plan.
create extension if not exists pg_cron;
create extension if not exists pg_net;

-- 2. Private bucket for the Fernet-encrypted originals. The API also creates it at startup; this guarantees public=false.
insert into storage.buckets (id, name, public, file_size_limit)
values ('medical-documents', 'medical-documents', false, 13631488)
on conflict (id) do update set public = false;
-- Deliberately NO storage.objects policies: with RLS on and zero policies, anon/authenticated keys can neither list,
-- read, write nor delete objects. Only the service-role key held by the Render backend can.

-- 3. Table lock-down is applied by the API itself on every start (api/cloud.py harden_database): RLS on all tables and
--    all privileges revoked from anon/authenticated. This query must return zero rows after the first deploy:
--    select tablename from pg_tables where schemaname='public' and not rowsecurity;

-- 4. Wake the sleeping free API every 10 minutes and run one reminder tick. The tick queries this database through the
--    pooler, which also counts as activity and keeps the free project from the 7-day inactivity pause.
select cron.unschedule('babycare-reminder-tick') where exists (select 1 from cron.job where jobname = 'babycare-reminder-tick');
select cron.schedule(
  'babycare-reminder-tick',
  '*/10 * * * *',
  $$
    select net.http_post(
      url := '__API_ORIGIN__/api/internal/scheduler/tick',
      headers := jsonb_build_object('X-Scheduler-Key', '__SCHEDULER_KEY__', 'Content-Type', 'application/json'),
      body := '{}'::jsonb,
      timeout_milliseconds := 90000
    ) as request_id
  $$
);

-- 5. Verify later:
--    select jobid, jobname, schedule, active from cron.job;
--    select status, created, (response).status_code from net._http_response order by created desc limit 5;
