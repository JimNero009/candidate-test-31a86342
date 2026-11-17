import calendar
import datetime
from collections import defaultdict

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Company, CompanyAdmin
from accounts.permissions import IsCompanyAdmin
from contracts.models import TimesheetEntry

from .enums import BillingRunStatus
from .models import BillingRun, Invoice, InvoiceLine


def _company_admin(request: Request) -> CompanyAdmin:
    # IsCompanyAdmin has already checked this; narrows the type and reuses its cached lookup.
    user = request.user
    if not isinstance(user, User):
        raise PermissionDenied
    return user.company_admin


# Possibly a contender for some more 'core' util, but here for now:
def _parse_month(raw: object) -> tuple[datetime.date, datetime.date]:
    if isinstance(raw, str):
        try:
            start = datetime.datetime.strptime(raw, '%Y-%m').date()
        except ValueError:
            pass
        else:
            _, last_day = calendar.monthrange(start.year, start.month)
            return start, start.replace(day=last_day)
    raise ValidationError({'month': 'Expected a month in YYYY-MM format.'})


class BillingRunView(APIView):
    permission_classes = [IsAuthenticated, IsCompanyAdmin]

    def post(self, request: Request) -> Response:
        admin = _company_admin(request)
        period_start, period_end = _parse_month(request.data.get('month'))
        if period_end >= timezone.localdate():
            raise ValidationError({'month': 'Only months that have ended can be billed.'})

        with transaction.atomic():
            # Serialises concurrent runs for the same company.
            company = Company.objects.select_for_update().get(pk=admin.company_id)
            run = BillingRun.objects.create(
                company=company,
                period_start=period_start,
                period_end=period_end,
                triggered_by=admin.user,
            )

            unbilled_entries = (
                TimesheetEntry.objects.filter(
                    contract__company=company,
                    status=TimesheetEntry.STATUS_APPROVED,
                    date__range=(period_start, period_end),
                    invoiceline__isnull=True,
                )
                .select_related('contract')
                .order_by('date')
            )
            entries_by_freelancer: defaultdict[int, list[TimesheetEntry]] = defaultdict(list)
            for entry in unbilled_entries:
                entries_by_freelancer[entry.contract.freelancer_id].append(entry)

            invoices = Invoice.objects.bulk_create(
                Invoice(
                    billing_run=run,
                    company=company,
                    freelancer_id=freelancer_id,
                    period_start=period_start,
                    period_end=period_end,
                )
                for freelancer_id in entries_by_freelancer
            )
            InvoiceLine.objects.bulk_create(
                InvoiceLine(
                    invoice=invoice,
                    timesheet_entry=entry,
                    date=entry.date,
                    hours=entry.hours,
                    daily_rate=entry.contract.daily_rate,
                )
                for invoice in invoices
                for entry in entries_by_freelancer[invoice.freelancer_id]
            )

            run.status = BillingRunStatus.COMPLETED
            run.save(update_fields=['status'])

        return Response(
            {
                'run_id': run.pk,
                'invoices_generated': len(invoices),
                'status': run.status,
            },
            status=status.HTTP_201_CREATED,
        )


class InvoiceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response([])


class InvoiceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
