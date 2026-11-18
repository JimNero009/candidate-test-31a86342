from rest_framework import serializers

from accounts.serializers import FreelancerSerializer

from .models import Invoice


class InvoiceSerializer(serializers.ModelSerializer[Invoice]):
    freelancer = FreelancerSerializer(read_only=True)
    total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            'id',
            'billing_run',
            'freelancer',
            'period_start',
            'period_end',
            'total',
            'created_at',
        ]
