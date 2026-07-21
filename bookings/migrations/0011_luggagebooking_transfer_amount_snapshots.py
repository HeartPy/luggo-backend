from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0010_luggagebooking_delivered_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='luggagebooking',
            name='transferred_gross_amount',
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                verbose_name='送金時の返金控除後売上額',
            ),
        ),
        migrations.AddField(
            model_name='luggagebooking',
            name='transferred_platform_fee',
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                verbose_name='送金時のプラットフォーム手数料',
            ),
        ),
    ]
