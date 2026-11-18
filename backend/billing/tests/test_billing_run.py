import datetime
from decimal import Decimal
from typing import NoReturn

import pytest
from django.contrib.auth.models import User
from django.db import DatabaseError
from freezegun import freeze_time
from rest_framework.test import APIClient

from billing.enums import BillingRunStatus
from billing.models import BillingRun, Invoice, InvoiceLine
from billing.tests.factories import make_contract, make_entry, make_freelancer
from contracts.models import Contract, TimesheetEntry


def _billed_entry_ids() -> set[int]:
    return set(InvoiceLine.objects.values_list('timesheet_entry_id', flat=True))


@pytest.mark.django_db
def test_run_creates_one_invoice_per_freelancer_with_a_line_per_entry(
    admin_client: APIClient, active_contract: Contract
) -> None:
    second_contract = make_contract(active_contract.company, make_freelancer())
    make_entry(active_contract, datetime.date(2026, 4, 1))
    make_entry(active_contract, datetime.date(2026, 4, 2), hours=Decimal('7.50'))
    make_entry(second_contract, datetime.date(2026, 4, 1))

    resp = admin_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    assert resp.json()['invoices_generated'] == 2
    invoice = Invoice.objects.get(freelancer=active_contract.freelancer)
    assert (invoice.period_start, invoice.period_end) == (
        datetime.date(2026, 4, 1),
        datetime.date(2026, 4, 30),
    )
    assert list(
        invoice.invoiceline_set.order_by('date').values_list('date', 'hours', 'daily_rate')
    ) == [
        (datetime.date(2026, 4, 1), Decimal('8.00'), Decimal('600.00')),
        (datetime.date(2026, 4, 2), Decimal('7.50'), Decimal('600.00')),
    ]
    second_invoice = Invoice.objects.get(freelancer=second_contract.freelancer)
    assert list(second_invoice.invoiceline_set.values_list('daily_rate', flat=True)) == [
        Decimal('400.00')
    ]


@pytest.mark.django_db
def test_freelancer_with_two_contracts_at_the_company_gets_one_invoice(
    admin_client: APIClient, active_contract: Contract
) -> None:
    second_engagement = make_contract(
        active_contract.company, active_contract.freelancer, daily_rate=Decimal('650.00')
    )
    make_entry(active_contract, datetime.date(2026, 4, 1))
    make_entry(second_engagement, datetime.date(2026, 4, 2))

    resp = admin_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    assert resp.json()['invoices_generated'] == 1
    invoice = Invoice.objects.get()
    assert list(invoice.invoiceline_set.order_by('date').values_list('daily_rate', flat=True)) == [
        Decimal('600.00'),
        Decimal('650.00'),
    ]


@pytest.mark.django_db
def test_run_only_bills_approved_entries_in_the_month_for_own_company(
    admin_client: APIClient, active_contract: Contract, other_contract: Contract
) -> None:
    billable = make_entry(active_contract, datetime.date(2026, 4, 1))
    make_entry(active_contract, datetime.date(2026, 4, 2), status=TimesheetEntry.STATUS_SUBMITTED)
    make_entry(active_contract, datetime.date(2026, 4, 3), status=TimesheetEntry.STATUS_DRAFT)
    make_entry(active_contract, datetime.date(2026, 4, 6), status=TimesheetEntry.STATUS_REJECTED)
    last_day = make_entry(active_contract, datetime.date(2026, 4, 30))
    make_entry(active_contract, datetime.date(2026, 3, 31))
    make_entry(active_contract, datetime.date(2026, 5, 1))
    make_entry(other_contract, datetime.date(2026, 4, 1))

    admin_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    assert _billed_entry_ids() == {billable.pk, last_day.pk}


@pytest.mark.django_db
def test_rerun_with_nothing_new_creates_no_invoices(
    admin_client: APIClient, active_contract: Contract
) -> None:
    make_entry(active_contract, datetime.date(2026, 4, 1))
    admin_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    resp = admin_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    assert resp.json()['invoices_generated'] == 0
    assert Invoice.objects.count() == 1


@pytest.mark.django_db
def test_entry_approved_after_run_goes_on_a_supplementary_invoice(
    admin_client: APIClient, active_contract: Contract
) -> None:
    make_entry(active_contract, datetime.date(2026, 4, 1))
    late = make_entry(
        active_contract, datetime.date(2026, 4, 2), status=TimesheetEntry.STATUS_SUBMITTED
    )
    admin_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    late.status = TimesheetEntry.STATUS_APPROVED
    late.save()
    resp = admin_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    supplementary = Invoice.objects.get(billing_run_id=resp.json()['run_id'])
    assert list(supplementary.invoiceline_set.values_list('timesheet_entry_id', flat=True)) == [
        late.pk
    ]


@pytest.mark.django_db
def test_run_returns_and_records_the_completed_run(
    admin_client: APIClient, admin_user: User, active_contract: Contract
) -> None:
    resp = admin_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    assert resp.status_code == 201
    body = resp.json()
    run = BillingRun.objects.get(pk=body['run_id'])
    assert body == {'run_id': run.pk, 'invoices_generated': 0, 'status': 'completed'}
    assert run.status == BillingRunStatus.COMPLETED
    assert run.company == active_contract.company
    assert run.triggered_by == admin_user
    assert (run.period_start, run.period_end) == (
        datetime.date(2026, 4, 1),
        datetime.date(2026, 4, 30),
    )


@pytest.mark.django_db
def test_run_period_covers_leap_day_in_february(admin_client: APIClient) -> None:
    resp = admin_client.post('/api/billing/runs/', {'month': '2024-02'}, format='json')

    run = BillingRun.objects.get(pk=resp.json()['run_id'])
    assert (run.period_start, run.period_end) == (
        datetime.date(2024, 2, 1),
        datetime.date(2024, 2, 29),
    )


@pytest.mark.django_db
@freeze_time('2026-05-31')
def test_run_rejects_month_that_has_not_started(admin_client: APIClient) -> None:
    resp = admin_client.post('/api/billing/runs/', {'month': '2026-06'}, format='json')

    assert resp.status_code == 400
    assert not BillingRun.objects.exists()


@pytest.mark.django_db
@freeze_time('2026-06-01')
def test_run_allows_month_from_its_first_day(admin_client: APIClient) -> None:
    resp = admin_client.post('/api/billing/runs/', {'month': '2026-06'}, format='json')

    assert resp.status_code == 201


@pytest.mark.django_db
@freeze_time('2026-05-15')
def test_run_bills_current_month_part_way_through(
    admin_client: APIClient, active_contract: Contract
) -> None:
    billed_so_far = make_entry(active_contract, datetime.date(2026, 5, 1))

    resp = admin_client.post('/api/billing/runs/', {'month': '2026-05'}, format='json')

    assert resp.status_code == 201
    assert _billed_entry_ids() == {billed_so_far.pk}


@pytest.mark.django_db
def test_run_that_fails_part_way_saves_nothing(
    admin_client: APIClient, active_contract: Contract, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_entry(active_contract, datetime.date(2026, 4, 1))

    def _fail(*args: object, **kwargs: object) -> NoReturn:
        raise DatabaseError('insert failed')

    monkeypatch.setattr(InvoiceLine.objects, 'bulk_create', _fail)

    with pytest.raises(DatabaseError):
        admin_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    assert not BillingRun.objects.exists()
    assert not Invoice.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    'payload', [{'month': 'April'}, {'month': '2026-13'}, {'month': '2026/04'}, {'month': ''}, {}]
)
def test_run_rejects_malformed_or_missing_month(
    admin_client: APIClient, payload: dict[str, str]
) -> None:
    resp = admin_client.post('/api/billing/runs/', payload, format='json')

    assert resp.status_code == 400
    assert not BillingRun.objects.exists()


@pytest.mark.django_db
def test_freelancer_cannot_run_billing(freelancer_client: APIClient) -> None:
    resp = freelancer_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    assert resp.status_code == 403


@pytest.mark.django_db
def test_anonymous_user_cannot_run_billing(api_client: APIClient) -> None:
    resp = api_client.post('/api/billing/runs/', {'month': '2026-04'}, format='json')

    assert resp.status_code == 401
