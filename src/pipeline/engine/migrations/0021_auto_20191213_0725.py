# -*- coding: utf-8 -*-
# Generated by Django 1.11.23 on 2019-12-13 07:25
from __future__ import unicode_literals

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('engine', '0020_pipelinemodel_priority'),
    ]

    operations = [
        migrations.AddField(
            model_name='pipelinemodel',
            name='queue',
            field=models.CharField(default='', max_length=512, verbose_name='流程使用的队列名'),
        ),
        migrations.AlterField(
            model_name='functionswitch',
            name='description',
            field=models.TextField(default='', verbose_name='功能描述'),
        ),
        migrations.AlterField(
            model_name='history',
            name='data',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='engine.HistoryData'),
        ),
        migrations.AlterField(
            model_name='nodecelerytask',
            name='celery_task_id',
            field=models.CharField(default='', max_length=40, verbose_name='celery 任务 ID'),
        ),
        migrations.AlterField(
            model_name='pipelineprocess',
            name='current_node_id',
            field=models.CharField(db_index=True, default='', max_length=32, verbose_name='当前推进到的节点的 ID'),
        ),
        migrations.AlterField(
            model_name='pipelineprocess',
            name='destination_id',
            field=models.CharField(default='', max_length=32, verbose_name='遇到该 ID 的节点就停止推进'),
        ),
        migrations.AlterField(
            model_name='pipelineprocess',
            name='parent_id',
            field=models.CharField(default='', max_length=32, verbose_name='父 process 的 ID'),
        ),
        migrations.AlterField(
            model_name='pipelineprocess',
            name='snapshot',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    to='engine.ProcessSnapshot'),
        ),
        migrations.AlterField(
            model_name='processcelerytask',
            name='celery_task_id',
            field=models.CharField(default='', max_length=40, verbose_name='celery 任务 ID'),
        ),
        migrations.AlterField(
            model_name='schedulecelerytask',
            name='celery_task_id',
            field=models.CharField(default='', max_length=40, verbose_name='celery 任务 ID'),
        ),
        migrations.AlterField(
            model_name='status',
            name='name',
            field=models.CharField(default='', max_length=64, verbose_name='节点名称'),
        ),
    ]