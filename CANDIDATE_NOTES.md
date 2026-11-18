# Candidate notes

## What I built and why

Task B, the monthly billing run. **TODO (Jim): why B over A and C.**

All three billing endpoints are now real: `POST /api/billing/runs/`, `GET /api/invoices/?month=`, `GET /api/invoices/<id>/`. Each run creates invoices from approved hours and emails each invoice to the company's `billing_email` and to the freelancer. Alongside that: a small amount of frontend polish (invoice list columns, a proper invoice page, showing the API's validation message) and 41 new backend tests.

## Key implementation notes

- **Approach.** I settled the data model first and let it shape the endpoints. No service layer: the existing codebase doesn't have one, so logic lives in views like the contracts app.
- **Data model.** `BillingRun` → `Invoice` (one per company × freelancer per run) → `InvoiceLine` (one per timesheet entry), plus `InvoiceEmail` (one per recipient). Invoices store `period_start`/`period_end` rather than a month, so non-monthly billing wouldn't need a schema change. Lines copy the entry's date and hours and the contract's day rate at billing time, so later edits can't change an issued invoice. The total is calculated as the sum of the lines, not stored. Foreign keys use `PROTECT`, because invoices are financial records.
- **Billing rule.** Each approved entry bills one day at the contract's `daily_rate`. That follows the README's domain (a fixed daily rate, one entry per day worked). The `ContractDetail` page's `hours / 8` estimate now disagrees with invoices.
- **Idempotency.** `InvoiceLine` has a OneToOne link to `TimesheetEntry`, so the database guarantees no entry is billed twice. A re-run only bills entries that aren't on an invoice yet: with nothing new it creates nothing, and a late approval goes on a supplementary invoice. Any month that has started can be billed. Future months are rejected.
- **Transactions.** A run is one transaction. It locks the company row so two runs can't overlap, and it uses `bulk_create`, so the query count is fixed whatever the number of freelancers. `bulk_create` returning primary keys depends on Postgres.
- **Email after commit.** Email rows are written as `pending` in the same transaction as the invoices, and only sent in `transaction.on_commit`. An email can never describe a rolled-back invoice. An SMTP failure after commit leaves the invoice in place, with the email marked `failed` and the error recorded. Re-running the month retries `pending`/`failed` emails, looking the address up again. Once `sent`, an email is never sent or re-addressed again. Delivery is at-least-once (a crash between the SMTP server accepting the message and the row being saved means a resend), and the invoice ID in the subject lets recipients spot duplicates.
- **Access.** A new `IsCompanyAdmin` permission in `accounts` (contracts views could adopt it). Another company's invoice returns 404 rather than 403, matching `ContractDetailView`.
- **Testing.** The tests cover filtering, month boundaries (including leap years and the first and last day), re-runs, supplementary invoices, rollback on failure, email ordering relative to commit, partial email failure, and retry addressing. I checked the tests by deliberately breaking the view in about twenty different ways, confirming each break failed a test. Concurrent runs aren't tested, because that needs a multi-threaded test against real transactions.
- **Tooling.** Ruff and strict mypy (with Django and DRF stubs) apply only to new code, to keep the diff focused.

## Workflow

**TODO (Jim): tools used, how you worked with them, and how the AI experience went.**

## Next steps (another four hours)

1. **Show email failures.** They're recorded but nothing displays them yet. Add an `emails_failed` count to the run response with a warning on the Billing page, and per-recipient email status on the invoice detail.
2. **Protect billed and approved data.** `PATCH /api/timesheets/<id>/` lets a freelancer approve their own entries, and lets anyone edit an entry after it's been billed. Restrict status changes to admins and lock billed entries.
3. **Make billing async.** `BillingRun` already has a status, so it can act as the job record. The POST would return 202, a worker would generate invoices and send `pending` emails, and the frontend would poll.
4. **Smaller follow-ups.** A `TimesheetEntryStatus` enum. Month boundaries in each company's time zone (they're UTC today). Copying the company's details onto the invoice. Invoice numbering, currency and VAT. Freelancers seeing their own invoices.
