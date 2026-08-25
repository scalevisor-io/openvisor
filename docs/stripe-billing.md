# Stripe billing, invoicing and tax

Everything about how money moves in this platform, what a customer receives afterwards, and the account setup that none of it works without. Read this before touching `services/stripe_svc.py` and before taking a new country live.

The short version: **Stripe owns the machinery, we own the facts.** Stripe has the rate tables, the invoice numbering, the PDF rendering and the filing reports. Stripe does not know that the operator is VAT-registered, what their registration number is, which jurisdictions they are obliged to collect in, or that the customer's legal name lives in our Postgres. None of that is inferred, and none of it is on by default. Everything below is us telling Stripe those facts.

There are no subscriptions. The wallet is prepaid credits, and every charge is a one-off.

## What a customer receives

Every payment produces a real invoice: a number, a PDF, a hosted page, the supplier's registration numbers, the buyer's legal name and address, and a tax line. Never a bare charge.

This matters beyond tidiness. A Stripe *receipt* carries no invoice number and no supplier registration number, so a business customer cannot reclaim the tax against one - the EU, the UK and Canada all want the supplier's number on the document. That is why Checkout runs with `invoice_creation`.

Two paths take money, and both invoice:

| | Credit top-up | Quote payment link |
| --- | --- | --- |
| Trigger | The customer clicks Continue to checkout on `/billing` | An admin quotes a request or a direct-quote project |
| Mechanism | Hosted Checkout against a saved Stripe customer | A Stripe PaymentLink, no customer until it is paid |
| Effect | Credits the wallet on `checkout.session.completed` | Marks the quote `paid` |
| Address used for tax | The account's saved address, prefilled and editable at Checkout | Whatever the payer types at Checkout |

`POST /api/billing/portal` returns a short-lived link into Stripe's own customer portal, offered from the Billing page once a payment has been taken. That is where a document corrected or credited after it was issued stays correct, which a copy of ours could not be. The portal runs on a configuration **we create** (`stripe_svc._portal_configuration`), never Stripe's default: a session created without one fails outright on an account where nobody has opened `Settings > Billing > Customer portal`, and the default configuration lets whoever opens it edit the payment method. Ours enables invoice history and nothing else.

The configuration is found by `metadata.openvisor`, for the same reason nothing here holds a Stripe id in an env var: an id means a different thing in sandbox than in live, so it cannot be a configuration value.

## The billing profile

`PATCH /api/account` writes it and `GET /api/account/countries` is the list the form renders. Four things travel to Stripe, and each has its own rule.

**The legal name** follows the account TYPE, not whichever field happens to be filled in. An organization that switches back to individual keeps its stored company details on purpose - a round-trip must not lose them - so reading `company_name or name` addressed a personal invoice to a company the account no longer claimed to be.

**The address** is withheld WHOLE when it is incomplete, never sent partial. A partial address resolves to the wrong tax rate rather than to an error: a US or Canadian one missing its state resolves to a federal rate and misses the other half of the bill. `countries.missing_address_fields` is the single definition of "incomplete", used both by `stripe_svc._address_of` (which decides whether to send) and by `org_out` (which tells the customer we hold nothing usable). If those two ever disagree, an account shows a complete profile on screen while Stripe receives nothing.

**A subdivision** goes only where the rate depends on one. Canada bills by province and the US by state; every EU member bills nationally, so asking a customer in Lisbon for a "province" is a field they cannot fill, and sending one to Germany is a subdivision Stripe Tax cannot place. Moving to a national-rate country clears it rather than refusing the change.

**The tax number** is organization-only, and unrecognisable input is withheld rather than guessed. `countries.tax_id_for` normalises the several spellings of the same number (a UK VAT number with or without its GB, an EIN with or without its hyphen, an EU number with or without its prefix - and Greece files under EL) and returns None for anything it cannot place. Stripe validates the format and rejects the call, and that call is in the middle of a payment: a wrong guess does not produce a wrong invoice, it takes the whole top-up down. The organization gate has teeth of its own - in the EU a customer VAT number moves the transaction to reverse charge and the invoice is issued with **no tax at all**, so a company number left behind on an account that has switched to individual would zero the VAT on a personal invoice.

`customer_update: {address: auto, name: auto}` means Checkout overwrites the Stripe customer with what the cardholder typed, and that is the address the invoice was computed and printed from - so `_adopt_billing_address` copies it back onto the org. Otherwise the account page shows one country while the filed invoice shows another. A country we do not bill in is not adopted: the payment stands, but the account would land in a state the form cannot express.

## Account setup: four things, per account, per mode

None of these live in this repo, because they are account identity rather than code, and **test and live accounts hold them separately**. Configuring the sandbox proves nothing about live.

1. **`Tax > Registrations`** - one per jurisdiction the operator must collect in. **Without any, nothing fails.** Checkout answers 200 and the tax line reads zero. The liability accrues silently until a filing, so the noise has to be ours: `stripe_svc.tax_registrations()` logs an error when the account has none, and `warn_if_not_registered_for` warns per sale when the buyer's country is not covered. Grep the api logs for those two before believing a quiet deployment is a correct one.
2. **`Settings > Business > Tax details`** - the operator's own registration numbers, as *account* tax IDs. `stripe_svc.account_tax_ids()` reads them and stamps them on every invoice. Read with `owner={"type": "self"}` - **not** `"account"`, which is for Connect and returns a 400. Missing them logs a warning on every call, because an invoice without a supplier number is one the customer cannot reclaim against.
3. **`Settings > Business > Customer emails > Successful payments`** - the toggle that actually mails the invoice. Test mode never sends these automatically; judge a test run by the Invoice object. Live does not send them either until this is on.
4. **`Developers > Webhooks` - an endpoint at `https://<app host>/api/billing/stripe/webhook`**, subscribed to `checkout.session.completed` and `invoice.paid`, with its signing secret in `STRIPE_WEBHOOK_SECRET`, followed by an api restart. **Without it, nothing fails.** Checkout answers 200, the card is charged, the customer is redirected to a success page, and no credit is ever granted: the wallet moves from the webhook and nowhere else. The sandbox never needs this step, because the `stripe-cli` sidecar relays every event - which is exactly why it is the step live does not get. Check with `stripe.WebhookEndpoint.list()` from the api pod, or on the dashboard; an empty list is this.

Also required for `automatic_tax`: a head office address under `Tax > Settings`. Without one the Checkout call fails with "You must have a valid head office address to enable automatic tax calculation". **This is the only part of the tax setup that fails loudly**, which is what makes the missing-registration case so easy to mistake for a working one: the call succeeds, so the setup looks done.

## Tax by region

Read `taxability_reason` off a real invoice rather than assuming. `standard_rated`, `reverse_charge` and `not_collecting` all look like a number on a balance sheet and mean entirely different things.

**European Union.** B2B is nearly free: a customer VAT number triggers `reverse_charge`, no VAT is charged, and the customer self-accounts. B2C is the expensive half - digital services sold to EU consumers owe VAT at the customer's national rate, filed through OSS. An EU-established seller also owes domestic VAT on domestic sales from its own registration.

**United Kingdom.** Its own registration post-Brexit, and the domestic threshold does not apply to a non-established seller.

**Switzerland and Norway.** Each has its own registration and its own threshold for foreign suppliers of digital services; neither is covered by an EU one.

**United States.** Per-state economic nexus, typically 100k USD or 200 transactions per state. Start with no registrations, let Stripe Tax's threshold monitoring say when one is crossed, and note that SaaS taxability varies by state. Until then, invoices read `not_collecting`, which is correct rather than broken.

**Canada.** Federal GST/HST is one registration and covers the HST provinces. Quebec is **not** included: QST belongs to Revenu Quebec and needs its own registration plus its own number, so a Quebec invoice with only the federal registration collects 5% and misses 9.975%.

**Prepaid credits.** Whether tax attaches when credits are *bought* or when they are *redeemed* depends on whether they are treated like a gift certificate. We tax at purchase. Worth confirming with an accountant, because the answer decides where the tax line belongs.

## Currency

**Stripe does not pick the currency; we hand it one.** `CREDIT_CURRENCY` is a single global and `stripe_svc` passes it as `currency=` on the Checkout line item, so an invoice reads EUR because that is what we asked for. A customer abroad can still pay it; their card network converts and their bank may add a spread. What they cannot do today is receive an invoice denominated in their own currency - that is a decision about what the wallet holds, not a Stripe setting.

## How the code is arranged

- `services/stripe_svc.py` - every Stripe call: the Checkout session, the customer profile sync, the account tax ids, the quote payment link, the read-only portal configuration, webhook signature verification.
- `services/countries.py` - which countries may hold an account, whether an address there needs a subdivision, and what a tax number entered on it means.
- `api/billing.py` - the webhook, the wallet ledger, the address write-back, and the portal link.
- `api/account.py` - the billing profile, validated against `countries.py`.

Rules worth not rediscovering:

- **Tax is exclusive.** The customer pays the top-up plus tax; the wallet is credited the pre-tax figure. Taking tax out of the credit would make every published rate wrong by the local percentage.
- **The invoice renders from the Stripe customer, not the session.** A profile that lives only in Postgres produces an invoice addressed to nobody.
- **`topup_ref` links the two webhooks.** Stripe issues the invoice after the payment settles, so the credit and the document arrive as separate objects with separate ids. The wallet moves on the first event; a balance must never wait on a PDF.
- **`total_taxes` is a list, and it is summed.** Rates stack - Quebec returns GST beside QST - so sampling the first under-reports. The flat `tax` field is the older spelling and reads as null on a current API version, which books a taxed invoice as untaxed.
- **A redelivered webhook must not credit twice.** `_credit_topup` checks for the session id in the ledger before writing. There is no unique constraint behind that check today, so it is a guard rather than a guarantee; Stripe's retries are seconds to minutes apart, which is what makes the guard sufficient in practice.

## Local development

`compose.dev.yml` runs a `stripe-cli` sidecar relaying sandbox webhooks whenever `STRIPE_SECRET_KEY` is a real `sk_` key; on a placeholder it exits 0 and stays down, every entry point raises `StripeUnavailable`, the API answers 503, and credits are granted by hand (`POST /api/admin/orgs/{id}/credits`). The listen session's signing secret is deterministic per API key, so set `STRIPE_WEBHOOK_SECRET` once from `stripe listen --print-secret`.

Several instances can share one sandbox and each receives every event. A webhook for an org id a database has never seen is logged and ignored.

To replay an event that failed - `stripe events resend` needs a registered endpoint and will not work against the CLI relay - fetch it with `stripe.Event.retrieve`, re-sign the payload with `STRIPE_WEBHOOK_SECRET` (`t=<ts>,v1=<hmac_sha256>`), and POST it to the webhook. That exercises the signature check too.
