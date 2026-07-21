import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business_owners', '0008_alter_businessprofile_stripe_account_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='PayoutDocumentDelivery',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ('stripe_payout_id', models.CharField(max_length=255, unique=True)),
                ('payout_created_at', models.DateTimeField()),
                ('arrival_date', models.DateField(blank=True, null=True)),
                ('payout_amount', models.IntegerField()),
                ('currency', models.CharField(default='jpy', max_length=3)),
                ('gross_sales', models.IntegerField(default=0)),
                ('platform_fee', models.IntegerField(default=0)),
                ('matched_transfer_amount', models.IntegerField(default=0)),
                ('adjustment_amount', models.IntegerField(default=0)),
                ('line_items', models.JSONField(default=list)),
                ('issuer_name', models.CharField(max_length=255)),
                ('issuer_address', models.TextField(blank=True, default='')),
                (
                    'issuer_registration_number',
                    models.CharField(blank=True, default='', max_length=14),
                ),
                ('recipient_email', models.EmailField(max_length=254)),
                ('send_attempts', models.PositiveIntegerField(default=0)),
                ('processing_started_at', models.DateTimeField(blank=True, null=True)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'business_profile',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='payout_document_deliveries',
                        to='business_owners.businessprofile',
                    ),
                ),
            ],
            options={
                'db_table': 'payout_document_deliveries',
                'ordering': ['-payout_created_at'],
            },
        ),
    ]
