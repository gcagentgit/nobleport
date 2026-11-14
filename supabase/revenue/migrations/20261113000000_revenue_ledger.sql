-- NoblePort Construction revenue ledger
-- Chain: job -> billing milestone -> approval -> Stripe invoice -> payment -> ledger -> dashboard
-- Construction rail only. No NBPT / investor funds are ever recorded here.
create extension if not exists pgcrypto;
create schema if not exists revenue;

create type revenue.milestone_kind as enum ('deposit','progress','awo','final');
create type revenue.milestone_status as enum
  ('draft','pending_approval','approved','rejected','invoiced','paid','void');

create table revenue.jobs (
  id uuid primary key default gen_random_uuid(),
  job_number text unique not null,
  hubspot_deal_id text,
  client_name text not null,
  client_email text not null,
  site_address text,
  contract_value_cents bigint not null check (contract_value_cents > 0),
  special_order_materials_cents bigint not null default 0 check (special_order_materials_cents >= 0),
  is_ma_hic boolean not null default true,          -- MA residential (M.G.L. c.142A) rules apply
  work_started_at timestamptz,
  completion_confirmed_by_owner boolean not null default false,
  completion_confirmed_by_contractor boolean not null default false,
  stripe_customer_id text,
  rail text not null default 'construction' check (rail = 'construction'),
  created_at timestamptz not null default now()
);

create table revenue.billing_milestones (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references revenue.jobs(id),
  kind revenue.milestone_kind not null,
  sequence int not null,
  description text not null,
  amount_cents bigint not null check (amount_cents > 0),
  status revenue.milestone_status not null default 'draft',
  required_approvals int not null default 1 check (required_approvals between 1 and 2),
  evidence_refs jsonb not null default '[]'::jsonb,   -- signed contract, AWO, photos, inspection
  stripe_invoice_id text unique,
  stripe_hosted_invoice_url text,
  created_by text not null default 'stephanie.ai',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (job_id, sequence)
);

create table revenue.approvals (
  id uuid primary key default gen_random_uuid(),
  milestone_id uuid not null references revenue.billing_milestones(id),
  approver text not null,
  decision text not null check (decision in ('approve','reject')),
  note text,
  decided_at timestamptz not null default now(),
  unique (milestone_id, approver)                     -- same person cannot count twice
);

create table revenue.stripe_events (
  id text primary key,                                -- Stripe event id = idempotency key
  type text not null,
  livemode boolean not null,
  payload jsonb not null,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  error text
);

create table revenue.payments (
  id uuid primary key default gen_random_uuid(),
  milestone_id uuid references revenue.billing_milestones(id),
  stripe_invoice_id text,
  stripe_payment_intent_id text,
  stripe_charge_id text,
  amount_cents bigint not null,
  currency text not null default 'usd',
  status text not null,                               -- paid, failed, refunded
  livemode boolean not null,
  occurred_at timestamptz not null default now(),
  stripe_event_id text references revenue.stripe_events(id),
  unique (stripe_event_id)
);

-- AuditBeacon: append-only, hash-chained
create table revenue.audit_events (
  seq bigserial primary key,
  occurred_at timestamptz not null default now(),
  actor text not null,
  action text not null,
  entity_type text not null,
  entity_id text,
  payload jsonb not null default '{}'::jsonb,
  prev_hash text,
  hash text not null
);

create or replace function revenue.audit_hash() returns trigger language plpgsql as $$
declare p text;
begin
  perform pg_advisory_xact_lock(hashtext('revenue.audit_events'));
  select hash into p from revenue.audit_events order by seq desc limit 1;
  new.prev_hash := coalesce(p, 'GENESIS');
  new.hash := encode(extensions.digest(new.prev_hash || '|' || new.occurred_at::text || '|' || new.actor || '|' ||
              new.action || '|' || new.entity_type || '|' || coalesce(new.entity_id,'') || '|' ||
              new.payload::text, 'sha256'), 'hex');
  return new;
end $$;
create trigger audit_hash before insert on revenue.audit_events
  for each row execute function revenue.audit_hash();

create or replace function revenue.audit_immutable() returns trigger language plpgsql as $$
begin raise exception 'audit_events is append-only'; end $$;
create trigger audit_no_update before update or delete on revenue.audit_events
  for each row execute function revenue.audit_immutable();

create or replace function revenue.audit(actor text, action text, etype text, eid text, payload jsonb)
returns void language sql as $$
  insert into revenue.audit_events(actor, action, entity_type, entity_id, payload)
  values (actor, action, etype, eid, coalesce(payload,'{}'::jsonb));
$$;

-- Verify the chain end to end
create or replace function revenue.verify_audit_chain() returns table(ok boolean, broken_at bigint)
language plpgsql as $$
declare r record; p text := 'GENESIS';
begin
  for r in select * from revenue.audit_events order by seq loop
    if r.prev_hash <> p or r.hash <> encode(extensions.digest(r.prev_hash || '|' || r.occurred_at::text || '|' || r.actor || '|' ||
       r.action || '|' || r.entity_type || '|' || coalesce(r.entity_id,'') || '|' || r.payload::text,'sha256'),'hex') then
      ok := false; broken_at := r.seq; return next; return;
    end if;
    p := r.hash;
  end loop;
  ok := true; broken_at := null; return next;
end $$;

-- Governance rules enforced in the database
create or replace function revenue.enforce_milestone_rules() returns trigger language plpgsql as $$
declare j revenue.jobs; approvals_n int; cap bigint; prior_deposits bigint;
begin
  select * into j from revenue.jobs where id = new.job_id;

  -- MA HIC c.142A s.2(a)(6): deposit before work <= greater of 1/3 contract or special-order materials
  if new.kind = 'deposit' and j.is_ma_hic then
    cap := greatest(j.contract_value_cents / 3, j.special_order_materials_cents);
    select coalesce(sum(amount_cents),0) into prior_deposits from revenue.billing_milestones
      where job_id = new.job_id and kind = 'deposit' and status <> 'void' and status <> 'rejected' and id <> new.id;
    if prior_deposits + new.amount_cents > cap then
      raise exception 'MA HIC deposit cap exceeded: % requested, cap % (greater of 1/3 contract or special-order materials)',
        prior_deposits + new.amount_cents, cap;
    end if;
  end if;

  -- Final payment cannot be demanded until completion is confirmed by both parties
  if new.kind = 'final' and new.status in ('approved','invoiced')
     and not (j.completion_confirmed_by_owner and j.completion_confirmed_by_contractor) then
    raise exception 'Final invoice blocked: completion not confirmed by both owner and contractor';
  end if;

  -- Cannot move to approved/invoiced without enough distinct human approvals
  if new.status in ('approved','invoiced') and old.status is distinct from new.status then
    select count(*) into approvals_n from revenue.approvals where milestone_id = new.id and decision = 'approve';
    if approvals_n < new.required_approvals then
      raise exception 'Milestone % needs % human approval(s), has %', new.id, new.required_approvals, approvals_n;
    end if;
  end if;

  -- AI actors may draft and request approval, never approve
  if new.status = 'approved' and coalesce(current_setting('revenue.actor', true),'') ilike '%.ai' then
    raise exception 'AI agents cannot approve billing milestones';
  end if;

  new.updated_at := now();
  return new;
end $$;
create trigger milestone_rules before insert or update on revenue.billing_milestones
  for each row execute function revenue.enforce_milestone_rules();

create or replace function revenue.audit_milestone() returns trigger language plpgsql as $$
begin
  perform revenue.audit(coalesce(nullif(current_setting('revenue.actor', true),''), new.created_by),
    case when tg_op = 'INSERT' then 'milestone.created' else 'milestone.' || new.status::text end,
    'billing_milestone', new.id::text,
    jsonb_build_object('job_id', new.job_id, 'kind', new.kind, 'amount_cents', new.amount_cents,
      'status', new.status, 'stripe_invoice_id', new.stripe_invoice_id));
  return new;
end $$;
create trigger milestone_audit after insert or update on revenue.billing_milestones
  for each row execute function revenue.audit_milestone();

create or replace function revenue.audit_approval() returns trigger language plpgsql as $$
begin
  if new.approver ilike '%.ai' then raise exception 'AI agents cannot record approvals'; end if;
  perform revenue.audit(new.approver, 'approval.' || new.decision, 'billing_milestone', new.milestone_id::text,
    jsonb_build_object('note', new.note));
  return new;
end $$;
create trigger approval_audit before insert on revenue.approvals
  for each row execute function revenue.audit_approval();

-- Webhook ingestion: idempotent, called by the stripe-webhook edge function
create or replace function revenue.ingest_stripe_event(evt jsonb) returns text
language plpgsql security definer set search_path = revenue, public as $$
declare obj jsonb := evt->'data'->'object'; mid uuid; inv text; t text := evt->>'type';
begin
  insert into revenue.stripe_events(id, type, livemode, payload)
  values (evt->>'id', t, (evt->>'livemode')::boolean, evt)
  on conflict (id) do nothing;
  if not found then return 'duplicate'; end if;

  inv := case when t like 'invoice.%' then obj->>'id' else obj->>'invoice' end;
  mid := coalesce(nullif(obj->'metadata'->>'milestone_id','')::uuid,
                  (select id from revenue.billing_milestones where stripe_invoice_id = inv));

  if t = 'invoice.paid' then
    insert into revenue.payments(milestone_id, stripe_invoice_id, stripe_payment_intent_id, amount_cents,
      currency, status, livemode, stripe_event_id)
    values (mid, inv, obj->>'payment_intent', (obj->>'amount_paid')::bigint, obj->>'currency', 'paid',
      (evt->>'livemode')::boolean, evt->>'id');
    perform set_config('revenue.actor', 'stripe.webhook', true);
    update revenue.billing_milestones set status = 'paid' where id = mid and status = 'invoiced';
  elsif t = 'invoice.payment_failed' then
    insert into revenue.payments(milestone_id, stripe_invoice_id, amount_cents, currency, status, livemode, stripe_event_id)
    values (mid, inv, (obj->>'amount_due')::bigint, obj->>'currency', 'failed', (evt->>'livemode')::boolean, evt->>'id');
  elsif t = 'charge.refunded' then
    insert into revenue.payments(milestone_id, stripe_invoice_id, stripe_charge_id, amount_cents, currency, status, livemode, stripe_event_id)
    values (mid, inv, obj->>'id', -(obj->>'amount_refunded')::bigint, obj->>'currency', 'refunded', (evt->>'livemode')::boolean, evt->>'id');
  elsif t = 'invoice.finalized' then
    update revenue.billing_milestones set stripe_hosted_invoice_url = obj->>'hosted_invoice_url'
      where id = mid;
  elsif t = 'invoice.voided' then
    perform set_config('revenue.actor', 'stripe.webhook', true);
    update revenue.billing_milestones set status = 'void' where id = mid;
  end if;

  perform revenue.audit('stripe.webhook', 'stripe.' || t, 'stripe_event', evt->>'id',
    jsonb_build_object('milestone_id', mid, 'invoice', inv, 'livemode', evt->'livemode'));
  update revenue.stripe_events set processed_at = now() where id = evt->>'id';
  return coalesce(mid::text, 'unmatched');
end $$;

-- Dashboard feed for Stephanie.ai
create or replace view revenue.dashboard as
select j.job_number, j.client_name, j.contract_value_cents,
  coalesce(sum(m.amount_cents) filter (where m.status in ('invoiced','paid')),0) as invoiced_cents,
  coalesce((select sum(p.amount_cents) from revenue.payments p join revenue.billing_milestones mm on mm.id=p.milestone_id
            where mm.job_id=j.id and p.status in ('paid','refunded')),0) as collected_cents,
  count(*) filter (where m.status = 'pending_approval') as awaiting_approval,
  count(*) filter (where m.status = 'invoiced') as open_invoices,
  max(m.updated_at) as last_activity
from revenue.jobs j left join revenue.billing_milestones m on m.job_id = j.id
group by j.id;

-- Lock down: service role only. Nothing exposed to anon/authenticated clients.
alter table revenue.jobs enable row level security;
alter table revenue.billing_milestones enable row level security;
alter table revenue.approvals enable row level security;
alter table revenue.stripe_events enable row level security;
alter table revenue.payments enable row level security;
alter table revenue.audit_events enable row level security;
revoke all on schema revenue from anon, authenticated;
grant usage on schema revenue to service_role;
grant all on all tables in schema revenue to service_role;
grant all on all sequences in schema revenue to service_role;
grant execute on all functions in schema revenue to service_role;
revoke execute on function revenue.ingest_stripe_event(jsonb) from public, anon, authenticated;

-- Webhook signing secret lives in Vault; only the edge function (service role) can read it
create or replace function public.get_stripe_webhook_secret() returns text
language sql security definer set search_path = vault, public as $$
  select decrypted_secret from vault.decrypted_secrets where name = 'stripe_webhook_secret' limit 1;
$$;
revoke execute on function public.get_stripe_webhook_secret() from public, anon, authenticated;
grant execute on function public.get_stripe_webhook_secret() to service_role;

create or replace function public.ingest_stripe_event(evt jsonb) returns text
language sql security definer set search_path = revenue, public as $$ select revenue.ingest_stripe_event(evt); $$;
revoke execute on function public.ingest_stripe_event(jsonb) from public, anon, authenticated;
grant execute on function public.ingest_stripe_event(jsonb) to service_role;
