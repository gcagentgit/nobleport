-- Qualify pgcrypto digest so audit hashing works under security-definer search_path (revenue, public)
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
