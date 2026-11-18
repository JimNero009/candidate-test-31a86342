import calendar
import datetime
from collections import defaultdict

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Prefetch, QuerySet, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Company, CompanyAdmin, Freelancer
from accounts.permissions import IsCompanyAdmin
from contracts.models import TimesheetEntry

from .enums import BillingRunStatus, InvoiceEmailRecipient
from .models import BillingRun, Invoice, InvoiceEmail, InvoiceLine
from .serializers import InvoiceDetailSerializer, InvoiceSerializer


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
        if period_start > timezone.localdate():
            raise ValidationError({'month': 'Months that have not started cannot be billed.'})

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
                .select_related('contract__freelancer__user')
                .order_by('date')
            )
            entries_by_freelancer: defaultdict[Freelancer, list[TimesheetEntry]] = defaultdict(list)
            for entry in unbilled_entries:
                entries_by_freelancer[entry.contract.freelancer].append(entry)

            invoices = Invoice.objects.bulk_create(
                Invoice(
                    billing_run=run,
                    company=company,
                    freelancer=freelancer,
                    period_start=period_start,
                    period_end=period_end,
                )
                for freelancer in entries_by_freelancer
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
                for entry in entries_by_freelancer[invoice.freelancer]
            )
            InvoiceEmail.objects.bulk_create(
                email
                for invoice in invoices
                for email in (
                    InvoiceEmail(
                        invoice=invoice,
                        recipient=InvoiceEmailRecipient.COMPANY,
                        to_address=company.billing_email,
                    ),
                    InvoiceEmail(
                        invoice=invoice,
                        recipient=InvoiceEmailRecipient.FREELANCER,
                        to_address=invoice.freelancer.user.email,
                    ),
                )
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


def _company_invoices(company_id: int) -> QuerySet[Invoice]:
    return (
        Invoice.objects.filter(company_id=company_id)
        .select_related('freelancer')
        .annotate(total=Sum('invoiceline__daily_rate'))
    )


class InvoiceListView(APIView):
    permission_classes = [IsAuthenticated, IsCompanyAdmin]

    def get(self, request: Request) -> Response:
        admin = _company_admin(request)
        period_start, period_end = _parse_month(request.query_params.get('month'))
        invoices = (
            _company_invoices(admin.company_id)
            .filter(period_start__lte=period_end, period_end__gte=period_start)
            .order_by('freelancer__name', 'id')
        )
        return Response(InvoiceSerializer(invoices, many=True).data)


class InvoiceDetailView(APIView):
    permission_classes = [IsAuthenticated, IsCompanyAdmin]

    def get(self, request: Request, pk: int) -> Response:
        admin = _company_admin(request)
        try:
            invoice = (
                _company_invoices(admin.company_id)
                .select_related('company')
                .prefetch_related(
                    Prefetch('invoiceline_set', queryset=InvoiceLine.objects.order_by('date', 'id'))
                )
                .get(pk=pk)
            )
        except Invoice.DoesNotExist:
            raise NotFound from None
        return Response(InvoiceDetailSerializer(invoice).data)
