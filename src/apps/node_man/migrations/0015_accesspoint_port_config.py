# Generated by Django 2.2.8 on 2020-09-23 06:20

from django.db import migrations
import django_mysql.models


class Migration(migrations.Migration):

    dependencies = [
        ("node_man", "0014_auto_20200818_1114"),
    ]

    operations = [
        migrations.AddField(
            model_name="accesspoint",
            name="port_config",
            field=django_mysql.models.JSONField(default=dict, verbose_name="GSE端口配置"),
        ),
    ]