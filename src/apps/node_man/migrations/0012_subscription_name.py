# Generated by Django 2.2.8 on 2020-07-28 07:30

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("node_man", "0011_processstatus_retry_times"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="name",
            field=models.CharField(blank=True, max_length=64, null=True, verbose_name="任务名称"),
        ),
    ]