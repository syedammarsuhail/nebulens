# Running NebuLens in hosted (multi-tenant) mode

This is for **operating NebuLens Cloud** — a hosted service you run for other people's orgs — not for self-hosting
NebuLens for your own team. If you just want to install NebuLens for yourself, see
[GETTING-STARTED.md](GETTING-STARTED.md) and [ON-PREM.md](ON-PREM.md) instead; everything below is inert unless
you explicitly turn hosted mode on.

Hosted mode's model: **organization → projects → API keys.** Each project is its own SQLite file (a hard data
silo between tenants). An org has an `ingest` key (what a customer's collectors use), a `read` key (dashboards,
read-only API use) and an `admin` key (manage the org's own projects/keys). You, the platform operator, hold a
separate `NEBULENS_ADMIN_TOKEN` that can create/inspect/delete any org.

## 1. Turn it on

```
NEBULENS_MODE=hosted
NEBULENS_ADMIN_TOKEN=<a long random value; openssl rand -hex 32>
```

The server refuses to start in hosted mode without `NEBULENS_ADMIN_TOKEN` set. Everything else (`NEBULENS_DB`,
`NEBULENS_DATA_DIR`, storage, the Helm chart, Docker image) works the same as a self-hosted install — hosted
mode changes the API surface and auth model, not how NebuLens is deployed.

## 2. Provisioning an org — two ways

**By hand, as the operator** (`/admin/*`, authenticated with `NEBULENS_ADMIN_TOKEN` as a Bearer token):

```bash
curl -X POST https://your-host/admin/orgs -H "Authorization: Bearer $NEBULENS_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "Acme Inc"}'
# -> {"id": "org_...", "name": "Acme Inc", "admin_key": {"key": "p0a_...", ...}}   <- shown once, save it
```
Also: `GET /admin/orgs`, `PATCH /admin/orgs/{id}` (rename, adjust `daily_cap`/`retention_hours`),
`DELETE /admin/orgs/{id}`, `POST /admin/orgs/{id}/keys`, `GET /admin/usage`.

**Self-serve, by the customer** (no admin token involved — see below).

## 3. Self-serve signup, login and accounts

```
POST /signup   {"email": "...", "password": "...", "org_name": "..."}
  -> {"org_id", "org_name", "email", "admin_key": {...}}   # admin_key.key is shown once, same as /admin/orgs
     also sets a signed, httponly nl_org_session cookie (30-day expiry)

POST /login     {"email": "...", "password": "..."}        -> sets the same cookie
POST /logout                                                -> clears it
GET  /account/me                                             -> org usage, tier, keys (needs the cookie)
```

New orgs start on the **free** tier (`HOSTED_TIER_QUOTAS["free"]` in `server.py`: `NEBULENS_DEFAULT_DAILY_CAP`
spans+logs+metric points/day, `NEBULENS_RETENTION_HOURS` retention).

**Known gap, not yet built:** there is no email verification and no rate limiting on `/signup` or `/login`.
Anyone can script unlimited free-tier signups, and `/login` has no lockout after repeated wrong passwords. If
you expose this publicly before that's addressed, put a rate limiter in front of it (your load balancer, a
reverse proxy, or a WAF rule) — don't rely on the app alone yet.

## 4. Stripe billing

```
NEBULENS_STRIPE_SECRET_KEY=sk_live_...                                  # or sk_test_... while testing
NEBULENS_STRIPE_WEBHOOK_SECRET=whsec_...                                # from the webhook endpoint below
NEBULENS_STRIPE_PRICE_TIERS={"price_abc123": "pro", "price_def456": "enterprise"}
```

Without `NEBULENS_STRIPE_SECRET_KEY`, `/billing/checkout` and `/billing/portal` answer `503 billing_not_configured`
— signup/login/account still work fine, orgs just can't start a paid subscription yet.

**One-time Stripe setup:**
1. Create a Product and one or more recurring Prices in the Stripe Dashboard (e.g. "Pro", "Enterprise").
2. Map each Price id to a tier name in `NEBULENS_STRIPE_PRICE_TIERS`. Starting Checkout with a `price_id` that
   isn't in this map is rejected with `400 unknown_price` — on purpose, so a price you forgot to map can't
   silently grant the wrong tier.
3. In the Stripe Dashboard, add a webhook endpoint pointing at `https://your-host/billing/webhook`, subscribed to
   at least `checkout.session.completed` and `customer.subscription.deleted` (and `customer.subscription.updated`
   for mid-cycle status changes). Copy its signing secret into `NEBULENS_STRIPE_WEBHOOK_SECRET`.

**Flow:**
```
POST /billing/checkout   {"price_id": "...", "success_url": "...", "cancel_url": "..."}   (needs the session cookie)
  -> {"url": "https://checkout.stripe.com/..."}        # redirect the browser here

POST /billing/portal                                                      (needs the session cookie)
  -> {"url": "https://billing.stripe.com/..."}         # self-service plan management/cancellation
     400 no_subscription if the org has never checked out yet

POST /billing/webhook                                    (Stripe calls this directly, no cookie/API key --
                                                           authenticity comes from the Stripe-Signature header)
```
A completed checkout moves the org to the mapped tier and updates its quotas immediately (same `daily_cap`/
`retention_hours` columns everything else already reads — nothing else needs to change for the new tier to take
effect). A canceled/deleted subscription reverts the org to `free`.

## 5. What's deliberately not built yet

- Self-serve signup/login rate limiting and email verification (see above).
- Any admin UI for `/admin/*` — it's an API only; build a small internal tool or use curl/Postman for now.
- Actual hosted infrastructure — this document assumes you've deployed the image somewhere with a real domain,
  TLS, and backups. None of that is provided by the repo itself.

## 6. Verifying a hosted deployment

```bash
curl https://your-host/api/mode
# -> {"mode": "hosted", ..., "features": {"multi_tenant": true, "organizations": true, ...}}

curl -X POST https://your-host/signup -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"a-real-password","org_name":"Test Org"}'
```
If that returns an `org_id` and an `admin_key`, hosted mode, signup and the platform database are all working.
