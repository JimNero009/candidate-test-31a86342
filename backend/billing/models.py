from django.db import models

from accounts.models import Company, Freelancer
from contracts.models import TimesheetEntry


class Invoice(models.Model):
    # PROTECT _for now_ -- a bit of a product decision regarding e.g. deletion of Company and Freelancer objects
    # So just take the view that Invoices are immutable financial objects that we need to do _something_ about, purposely
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    freelancer = models.ForeignKey(Freelancer, on_delete=models.PROTECT)

    # Arguing with the AI about this. It wanted to store a DateField only, but I didn't like that:
    # a) That would actually be stored as some day of the month, with an arbitary choice with no real good reason
    # b) I can easily imagine contractors aren't always going to be billed at the same cadence, so I wanted something
    # with a bit of flexibility to e.g. an API change to accept a start and end date.
    period_start = models.DateField()
    period_end = models.DateField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F('period_start')),
                name='invoice_period_end_after_start',
            ),
        ]

    def __str__(self) -> str:
        return f'Invoice {self.pk}: {self.freelancer} @ {self.company} ({self.period_start} – {self.period_end})'


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE)
    timesheet_entry = models.OneToOneField(TimesheetEntry, on_delete=models.PROTECT)

    # Each entry bills as one day at daily_rate.
    date = models.DateField()
    # Match decimal fields in TimesheetEntry and Contract:
    hours = models.DecimalField(max_digits=5, decimal_places=2)
    daily_rate = models.DecimalField(max_digits=8, decimal_places=2)

    def __str__(self) -> str:
        return f'Invoice {self.invoice_id} line: {self.date} @ {self.daily_rate}'
