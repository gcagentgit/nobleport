// NoblePort Construction revenue: Stripe webhook -> revenue ledger + AuditBeacon
// Verifies the Stripe signature, then records the event idempotently.
// Read/record only: this function never creates, sends, or charges anything.
import Stripe from "npm:stripe@17.7.0";
import { createClient } from "npm:@supabase/supabase-js@2.45.4";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);
const stripe = new Stripe("sk_unused_for_verification", { httpClient: Stripe.createFetchHttpClient() });
const crypto = Stripe.createSubtleCryptoProvider();

let cachedSecret: string | null = null;
async function webhookSecret(): Promise<string> {
  if (cachedSecret) return cachedSecret;
  const env = Deno.env.get("STRIPE_WEBHOOK_SECRET");
  if (env) return (cachedSecret = env);
  const { data, error } = await supabase.rpc("get_stripe_webhook_secret");
  if (error || !data) throw new Error("webhook secret unavailable");
  return (cachedSecret = data as string);
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const sig = req.headers.get("stripe-signature");
  if (!sig) return new Response("missing signature", { status: 400 });

  const body = await req.text();
  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(body, sig, await webhookSecret(), undefined, crypto);
  } catch (e) {
    console.error("signature verification failed", (e as Error).message);
    return new Response("invalid signature", { status: 400 }); // fail closed
  }

  const { data, error } = await supabase.rpc("ingest_stripe_event", { evt: JSON.parse(body) });
  if (error) {
    console.error("ingest failed", event.id, error.message);
    return new Response("ingest failed", { status: 500 }); // Stripe will retry
  }
  return new Response(JSON.stringify({ received: true, event: event.id, result: data }), {
    headers: { "content-type": "application/json" },
  });
});
