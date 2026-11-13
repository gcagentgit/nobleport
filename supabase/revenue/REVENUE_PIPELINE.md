# NoblePort Construction revenue pipeline

Supabase project: `nobleport-construction-revenue` (`iugtaainttnnbawnvfpl`, us-east-1). Construction rail only;
no NBPT or investor funds are recorded here.

## Flow
1. **Milestone drafted** (`revenue.billing_milestones`, status `draft` → `pending_approval`) with evidence refs
   (signed contract, AWO, inspection, photos). Stephanie.ai may draft; it cannot approve.
2. **Human approval** (`revenue.approvals`). The database blocks `approved`/`invoiced` until
   `required_approvals` distinct humans approve (1 or 2). Approvers ending in `.ai` are rejected.
3. **Invoice** – `create-invoice` edge function creates + finalizes a Stripe invoice for an approved milestone
   (does not email the customer). Requires Supabase secret `STRIPE_SECRET_KEY`.
4. **Payment** – customer pays via the Stripe hosted invoice page.
5. **Ledger** – `stripe-webhook` edge function verifies the Stripe signature (secret in Vault:
   `stripe_webhook_secret`), stores the event idempotently in `revenue.stripe_events`, writes
   `revenue.payments`, and moves the milestone to `paid`.
6. **AuditBeacon** – every milestone change, approval, and Stripe event is appended to
   `revenue.audit_events` (SHA-256 hash chain, append-only). Check with `select * from revenue.verify_audit_chain();`
7. **Dashboard** – `revenue.dashboard` view: contract value, invoiced, collected, awaiting approval, open invoices.

## Rules enforced in the database
- **MA HIC (M.G.L. c.142A §2(a)(6))**: total deposits ≤ greater of 1/3 contract price or special-order materials cost.
- **Final payment** blocked until completion is confirmed by both owner and contractor.
- Webhook fails closed on missing/invalid signatures (HTTP 400).

## Stripe (test mode)
Webhook endpoint `we_1Ub9IS9qtdE10RahxoOSqJld` → `https://iugtaainttnnbawnvfpl.supabase.co/functions/v1/stripe-webhook`
Events: invoice.finalized, invoice.paid, invoice.payment_failed, invoice.voided, charge.refunded.

## Going live (requires explicit approval)
- Create a live-mode webhook endpoint and store its secret in Vault (separate name or replace).
- Set `STRIPE_SECRET_KEY` to a live restricted key.
- Remove test job `TEST-0001` from the ledger.
