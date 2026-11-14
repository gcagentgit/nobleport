// NoblePort Construction revenue: create + finalize a Stripe invoice for an APPROVED milestone.
// Fail-closed: refuses unless the milestone is 'approved' (which the database only allows
// after the required number of distinct human approvals). Does not email the customer;
// sending stays a separate human action in Stripe.
// Requires Supabase secret STRIPE_SECRET_KEY (restricted key: invoices, invoice items, customers write).
import Stripe from "npm:stripe@17.7.0";
import { createClient } from "npm:@supabase/supabase-js@2.45.4";

const supabase = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false }, db: { schema: "revenue" } });

Deno.serve(async (req) => {
  const key = Deno.env.get("STRIPE_SECRET_KEY");
  if (!key) return new Response("STRIPE_SECRET_KEY not configured", { status: 503 });
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const { milestone_id } = await req.json().catch(() => ({}));
  if (!milestone_id) return new Response("milestone_id required", { status: 400 });

  const stripe = new Stripe(key, { httpClient: Stripe.createFetchHttpClient() });
  const { data: m } = await supabase.from("billing_milestones").select("*, jobs(*)").eq("id", milestone_id).single();
  if (!m) return new Response("milestone not found", { status: 404 });
  if (m.status !== "approved") return new Response(`milestone is '${m.status}', not approved`, { status: 409 });

  const job = m.jobs;
  let customer = job.stripe_customer_id;
  if (!customer) {
    customer = (await stripe.customers.create({ name: job.client_name, email: job.client_email,
      metadata: { job_number: job.job_number, rail: "construction" } })).id;
    await supabase.from("jobs").update({ stripe_customer_id: customer }).eq("id", job.id);
  }
  const meta = { milestone_id: m.id, job_number: job.job_number, kind: m.kind, rail: "construction" };
  const inv = await stripe.invoices.create({ customer, collection_method: "send_invoice", days_until_due: 7,
    auto_advance: false, description: `NoblePort Construction - ${job.job_number} ${m.description}`, metadata: meta },
    { idempotencyKey: `inv-${m.id}` });
  await stripe.invoiceItems.create({ customer, invoice: inv.id, amount: m.amount_cents, currency: "usd",
    description: `${m.description} - ${job.site_address ?? ""}`.trim() }, { idempotencyKey: `ii-${m.id}` });
  const fin = await stripe.invoices.finalizeInvoice(inv.id, { auto_advance: false });

  const { error } = await supabase.from("billing_milestones").update({ status: "invoiced",
    stripe_invoice_id: fin.id, stripe_hosted_invoice_url: fin.hosted_invoice_url }).eq("id", m.id);
  if (error) return new Response(`invoice ${fin.id} created but ledger update failed: ${error.message}`, { status: 500 });
  return Response.json({ invoice: fin.id, hosted_invoice_url: fin.hosted_invoice_url, status: fin.status });
});
