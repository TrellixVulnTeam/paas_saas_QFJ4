# Generated by Django 2.2.8 on 2020-06-04 13:07

import apps.node_man.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("node_man", "0006_auto_20200602_1104"),
    ]

    operations = [
        migrations.AlterField(
            model_name="host", name="ap_id", field=models.IntegerField(db_index=True, null=True, verbose_name="接入点ID"),
        ),
        migrations.AlterField(
            model_name="host", name="bk_biz_id", field=models.IntegerField(db_index=True, verbose_name="业务ID"),
        ),
        migrations.AlterField(
            model_name="host", name="bk_cloud_id", field=models.IntegerField(db_index=True, verbose_name="云区域ID"),
        ),
        migrations.AlterField(
            model_name="host",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="创建时间"),
        ),
        migrations.AlterField(
            model_name="host",
            name="inner_ip",
            field=models.CharField(db_index=True, max_length=45, verbose_name="内网IP"),
        ),
        migrations.AlterField(
            model_name="host",
            name="node_type",
            field=models.CharField(
                choices=[("AGENT", "AGENT"), ("PROXY", "PROXY"), ("PAGENT", "PAGENT")],
                db_index=True,
                max_length=45,
                verbose_name="节点类型",
            ),
        ),
        migrations.AlterField(
            model_name="host",
            name="os_type",
            field=models.CharField(
                choices=[("LINUX", "LINUX"), ("WINDOWS", "WINDOWS"), ("AIX", "AIX")],
                db_index=True,
                default="linux",
                max_length=45,
                verbose_name="操作系统",
            ),
        ),
        migrations.AlterField(
            model_name="host",
            name="updated_at",
            field=models.DateTimeField(db_index=True, null=True, verbose_name="更新时间"),
        ),
        migrations.AlterField(
            model_name="jobtask", name="bk_host_id", field=models.IntegerField(db_index=True, verbose_name="主机ID"),
        ),
        migrations.AlterField(
            model_name="jobtask", name="create_time", field=models.DateTimeField(auto_now_add=True, db_index=True),
        ),
        migrations.AlterField(
            model_name="jobtask",
            name="instance_id",
            field=models.CharField(db_index=True, max_length=45, verbose_name="实例ID"),
        ),
        migrations.AlterField(
            model_name="jobtask", name="job_id", field=models.IntegerField(db_index=True, verbose_name="作业ID"),
        ),
        migrations.AlterField(
            model_name="jobtask",
            name="pipeline_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=50, verbose_name="Pipeline节点ID"),
        ),
        migrations.AlterField(
            model_name="jobtask", name="update_time", field=models.DateTimeField(auto_now=True, db_index=True),
        ),
        migrations.AlterField(
            model_name="pipelinetree",
            name="tree",
            field=apps.node_man.models.LazyJSONField(default=dict, verbose_name="Pipeline拓扑树"),
        ),
        migrations.AlterField(
            model_name="pluginconfiginstance",
            name="create_time",
            field=models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="创建时间"),
        ),
        migrations.AlterField(
            model_name="pluginconfiginstance",
            name="source_app_code",
            field=models.CharField(db_index=True, max_length=64, verbose_name="来源系统app code"),
        ),
        migrations.AlterField(
            model_name="pluginconfigtemplate",
            name="is_main",
            field=models.BooleanField(db_index=True, default=False, verbose_name="是否主配置"),
        ),
        migrations.AlterField(
            model_name="pluginconfigtemplate",
            name="is_release_version",
            field=models.BooleanField(db_index=True, verbose_name="是否已经发布版本"),
        ),
        migrations.AlterField(
            model_name="pluginconfigtemplate",
            name="name",
            field=models.CharField(db_index=True, max_length=128, verbose_name="配置模板名"),
        ),
        migrations.AlterField(
            model_name="pluginconfigtemplate",
            name="plugin_version",
            field=models.CharField(db_index=True, max_length=128, verbose_name="版本号"),
        ),
        migrations.AlterField(
            model_name="pluginconfigtemplate",
            name="version",
            field=models.CharField(db_index=True, max_length=128, verbose_name="配置模板版本"),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="create_time",
            field=models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="创建时间"),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="creator",
            field=models.CharField(db_index=True, max_length=64, verbose_name="操作人"),
        ),
        migrations.AlterField(
            model_name="subscription", name="is_deleted", field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="node_type",
            field=models.CharField(
                choices=[("TOPO", "拓扑"), ("INSTANCE", "实例"), ("INSTANCE", "实例")],
                db_index=True,
                max_length=20,
                verbose_name="节点类型",
            ),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="object_type",
            field=models.CharField(
                choices=[("HOST", "主机"), ("SERVICE", "服务")], db_index=True, max_length=20, verbose_name="对象类型"
            ),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="update_time",
            field=models.DateTimeField(auto_now=True, db_index=True, verbose_name="更新时间"),
        ),
        migrations.AlterField(
            model_name="subscriptioninstancerecord",
            name="create_time",
            field=models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="创建时间"),
        ),
        migrations.AlterField(
            model_name="subscriptioninstancerecord",
            name="is_latest",
            field=models.BooleanField(db_index=True, default=True, verbose_name="是否为实例最新记录"),
        ),
        migrations.AlterField(
            model_name="subscriptioninstancerecord",
            name="pipeline_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=50, verbose_name="Pipeline ID"),
        ),
        migrations.AlterField(
            model_name="subscriptioninstancerecord",
            name="update_time",
            field=models.DateTimeField(auto_now=True, db_index=True, verbose_name="更新时间"),
        ),
        migrations.AlterField(
            model_name="subscriptionstep",
            name="step_id",
            field=models.CharField(db_index=True, max_length=64, verbose_name="步骤ID"),
        ),
        migrations.AlterField(
            model_name="subscriptiontask",
            name="create_time",
            field=models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="创建时间"),
        ),
    ]