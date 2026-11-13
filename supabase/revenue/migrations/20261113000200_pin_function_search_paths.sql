-- Pin search_path on all revenue functions (Supabase security advisor: function_search_path_mutable)
do $$ declare f record; begin
for f in select p.oid::regprocedure sig from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='revenue' and p.prokind='f' and (p.proconfig is null) loop
  execute format('alter function %s set search_path = revenue, extensions, public', f.sig); end loop; end $$;
alter function public.get_stripe_webhook_secret() set search_path = vault, public;
alter function public.ingest_stripe_event(jsonb) set search_path = revenue, extensions, public;
