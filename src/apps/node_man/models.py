# -*- coding: utf-8 -*-
import base64
import copy
import errno
import hashlib
import json
import os
import random
import shutil
import subprocess
import tarfile
import traceback
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import six
import yaml
from Cryptodome.Cipher import AES
from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.db import transaction
from django.db.models import DateTimeField
from django.utils import timezone
from django.utils.encoding import force_text
from django.utils.functional import Promise
from django.utils.translation import ugettext_lazy as _, ugettext
from django_mysql.models import JSONField
from jinja2 import Template

from apps.backend.subscription.errors import PipelineExecuteFailed
from apps.backend.utils.data_renderer import nested_render_data
from apps.node_man import constants as const, constants
from apps.node_man.exceptions import AliveProxyNotExistsError, ApIDNotExistsError
from apps.utils import env
from apps.utils.basic import md5
from common.log import logger
from pipeline.parser import PipelineParser
from pipeline.service import task_service


class LazyJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Promise):
            return force_text(obj)
        return super(LazyJSONEncoder, self).default(obj)


class LazyJSONField(JSONField):
    _default_json_encoder = LazyJSONEncoder(allow_nan=False)


class GlobalSettings(models.Model):
    """
    配置表
    """

    key = models.CharField(_("键"), max_length=255, db_index=True, primary_key=True)
    v_json = JSONField(_("值"))

    def map_values(self, objs, source, target):
        """
        列表内字典转一键多值字典
        """

        ret = {}
        for obj in objs:
            ret[source(obj)] = target(obj)
        return ret

    def fetch_isp(self):
        isps = dict(GlobalSettings.objects.filter(key="isp").values_list("key", "v_json")).get("isp", [])
        result = self.map_values(
            isps, lambda isp: isp["isp"], lambda isp: {"isp_name": isp["isp_name"], "isp_icon": isp["isp_icon"]}
        )

        return result

    @classmethod
    def get_config(cls, key=None, default=None):
        try:
            return cls.objects.get(key=key).v_json
        except cls.DoesNotExist:
            return default

    @classmethod
    def set_config(cls, key, value):
        cls.objects.create(key=key, v_json=value)

    @classmethod
    def update_config(cls, key, value):
        cls.objects.filter(key=key).update(v_json=value)

    class Meta:
        verbose_name = _("配置表")
        verbose_name_plural = _("配置表")


class AESCipher(object):
    """
    AES256加解密器
    注意事项：
    - 加密后密文长度大雨明文长度
    - iv默认使用无需设置，设置后会固定iv进行加密
    - 使用的实际秘钥，是对设置的key进行sha256后的结果，无论输入的key多长，实际秘钥固定为32位，使用AES256算法
    """

    def __init__(self, instance_key=settings.SECRET_KEY):
        try:
            obj, created = GlobalSettings.objects.get_or_create(
                # 存储密钥，以便于后台使用同个KEY解密
                key="null",
                defaults={"v_json": base64.b64encode(instance_key.encode()).decode()},
            )
        except Exception as e:
            # 初次migrate时，GlobalSettings不存在
            logger.error(_("获取密钥失败：{e}").format(e=e))
            instance_key = base64.b64encode(instance_key.encode()).decode()
        else:
            instance_key = obj.v_json
        self.iv = b"TencentBkNode-Iv"
        self.key = instance_key
        self.bs = 3

    def encrypt(self, plaintext):
        """
        加密
        :param plaintext: 需要加密的内容
        :return:
        """
        decrypt_key = self.__parse_key()
        plaintext = self._pad(plaintext)
        secret_txt = AES.new(decrypt_key, AES.MODE_CFB, self.iv).encrypt(plaintext.encode())
        return base64.b64encode(secret_txt).decode("utf-8")

    def decrypt(self, ciphertext):
        """
        解密
        :param ciphertext: 需要解密的内容
        :return:
        """
        decrypt_key = self.__parse_key()
        # 先解base64
        secret_txt = base64.b64decode(ciphertext)
        # 再解对称加密
        try:
            plain = AES.new(decrypt_key, AES.MODE_CFB, self.iv).decrypt(secret_txt)
        except ValueError as error:
            logger.error(f"[decrypt error]: [decrypt_key]={decrypt_key}, secret_txt={secret_txt}, error={error}")
            raise error
        plain = plain.decode(encoding="utf-8")
        return self._unpad(plain)

    def __parse_key(self):
        return self.key[:24].encode()

    def _pad(self, s):
        """
        打包成长度为bs整数倍的字符串
        """
        return s + (self.bs - len(s) % self.bs) * chr(self.bs - len(s) % self.bs)

    @staticmethod
    def _unpad(s):
        """
        解包成原文本
        """
        return s[: -ord(s[len(s) - 1 :])]


# 单例
aes_cipher = AESCipher()


class AESTextField(models.TextField):
    """
    在数据库中AES256加密的 TextField
    - 兼容未加密数据，加密后字符串会带上
    """

    def __init__(self, *args, **kwargs):
        """
        初始化
        :param prefix: 加密串前缀
        """
        if "prefix" in kwargs:
            self.prefix = kwargs["prefix"]
            del kwargs["prefix"]
        else:
            self.prefix = "aes_str:::"

        self.cipher = aes_cipher
        super(AESTextField, self).__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super(AESTextField, self).deconstruct()
        if self.prefix != "aes_str:::":
            kwargs["prefix"] = self.prefix
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection, context):
        """
        出库后解密数据
        """
        if value is None:
            return value
        if value.startswith(self.prefix):
            value = value[len(self.prefix) :]
            value = self.cipher.decrypt(value)

        return value

    def to_python(self, value):
        """
        反序列化和Form clean()时调用，解密数据
        """
        if value is None:
            return value
        elif value.startswith(self.prefix):
            value = value[len(self.prefix) :]
            value = self.cipher.decrypt(value)

        return value

    def get_prep_value(self, value):
        """
        入库前加密数据
        """
        if isinstance(value, str) or isinstance(value, str):
            value = self.cipher.encrypt(value)
            value = self.prefix + value
        elif value is not None:
            raise TypeError(str(value) + " is not a valid value for TextCharField")

        return value


class IdentityData(models.Model):
    bk_host_id = models.IntegerField(_("主机ID"), primary_key=True)
    auth_type = models.CharField(_("认证类型"), max_length=45, choices=const.AUTH_CHOICES, default=const.AuthType.PASSWORD)
    account = models.CharField(_("账户名"), max_length=45, default="")
    password = AESTextField(_("密码"), blank=True, null=True)
    port = models.IntegerField(_("端口"), null=True, default=22)
    key = AESTextField(_("密钥"), blank=True, null=True)
    extra_data = JSONField(_("额外认证资料"), blank=True, null=True)
    retention = models.IntegerField(_("保留天数"), default=1)
    updated_at = models.DateTimeField(_("更新时间"), null=True, auto_now=False)

    class Meta:
        verbose_name = _("主机认证信息")
        verbose_name_plural = _("主机认证信息")


class Host(models.Model):
    bk_host_id = models.IntegerField(_("CMDB主机ID"), primary_key=True)
    bk_biz_id = models.IntegerField(_("业务ID"), db_index=True)
    bk_cloud_id = models.IntegerField(_("云区域ID"), db_index=True)

    inner_ip = models.CharField(_("内网IP"), max_length=45, db_index=True)
    outer_ip = models.CharField(_("外网IP"), max_length=45, blank=True, null=True, default="")
    login_ip = models.CharField(_("登录IP"), max_length=45, blank=True, null=True, default="")
    data_ip = models.CharField(_("数据IP"), max_length=45, blank=True, null=True, default="")

    os_type = models.CharField(
        _("操作系统"), max_length=45, choices=const.OS_CHOICES, default=const.OsType.LINUX, db_index=True
    )
    node_type = models.CharField(_("节点类型"), max_length=45, choices=const.NODE_CHOICES, db_index=True)
    node_from = models.CharField(_("节点来源"), max_length=45, choices=const.NODE_FROM_CHOICES, default="NODE_MAN")
    is_manual = models.BooleanField(_("是否手动安装"), default=False)

    ap_id = models.IntegerField(_("接入点ID"), null=True, db_index=True)
    upstream_nodes = JSONField(_("上游节点"), default=list)

    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("更新时间"), null=True, auto_now=False, db_index=True)

    extra_data = JSONField(_("额外数据"), blank=True, null=True)

    @classmethod
    def get_by_host_info(cls, host_info):
        """
        根据 host_info 获取 Host 对象
        :param host_info: [
            {
                "ip": "10.0.0.1",
                "bk_cloud_id": 0,
                "bk_supplier_id": 0
            }
        ]
        or {
                "bk_host_id": 1
            }
        or Host Object
        """
        if isinstance(host_info, Host):
            return host_info

        bk_host_id = host_info.get("bk_host_id")
        if bk_host_id:
            try:
                return Host.objects.get(bk_host_id=bk_host_id,)
            except Host.DoesNotExist:
                exception = _("{bk_host_id}| 主机信息不存在").format(bk_host_id=bk_host_id)
        else:
            ip = host_info.get("bk_host_innerip") or host_info.get("ip")
            # 兼容IP为逗号分割的多IP情况，取第一个IP
            ip = ip.split(",")[0]
            bk_cloud_id = host_info["bk_cloud_id"]

            try:
                return Host.objects.get(inner_ip=ip, bk_cloud_id=bk_cloud_id,)
            except Host.DoesNotExist:
                exception = _("{ip}|{bk_cloud_id} 主机信息不存在").format(ip=ip, bk_cloud_id=bk_cloud_id)

        raise Exception(exception)

    def get_random_alive_proxy(self):
        """
        随机选一台可用的proxy
        """
        proxy_ids = self.proxies.values_list("bk_host_id", flat=True)
        alive_proxies = ProcessStatus.objects.filter(
            bk_host_id__in=proxy_ids, name=ProcessStatus.GSE_AGENT_PROCESS_NAME, status=const.ProcStateType.RUNNING
        )
        if not alive_proxies:
            raise AliveProxyNotExistsError(_("主机所属云区域不存在可用Proxy"))
        else:
            proxy_id = random.choice(alive_proxies).bk_host_id
            return Host.objects.get(bk_host_id=proxy_id)

    @staticmethod
    def get_cpu_arch_by_os(os_type):
        """
        根据操作系统返回默认系统架构
        :param os_type: 操作系统(aix, linux, windows)
        :return: cpu_arch(powerpc, x86_64)
        """
        os_type = os_type.lower()
        if os_type == constants.OsType.AIX.lower():
            cpu_arch = constants.CpuType.powerpc
        else:
            cpu_arch = constants.CpuType.x86_64
        return cpu_arch

    @property
    def identity(self) -> IdentityData:

        if not getattr(self, "_identity", None):
            self._identity, created = IdentityData.objects.get_or_create(bk_host_id=self.bk_host_id)
        return self._identity

    @property
    def ap(self):
        if not getattr(self, "_ap", None):
            # 未选择接入点时，默认取第一个接入点
            if self.ap_id == const.DEFAULT_AP_ID:
                ap = AccessPoint.objects.first()
                if ap:
                    self._ap = ap
                else:
                    raise ApIDNotExistsError
            else:
                try:
                    self._ap = AccessPoint.objects.get(pk=self.ap_id)
                except AccessPoint.DoesNotExist:
                    raise ApIDNotExistsError
        return self._ap

    @property
    def agent_config(self):
        os_type = self.os_type.lower()
        # AIX与Linux共用配置
        if self.os_type == const.OsType.AIX:
            os_type = const.OsType.LINUX.lower()
        return self.ap.agent_config[os_type]

    @property
    def proxies(self):
        return Host.objects.filter(bk_cloud_id=self.bk_cloud_id, node_type=const.NodeType.PROXY,)

    class Meta:
        verbose_name = _("主机信息")
        verbose_name_plural = _("主机信息")
        ordering = ["-updated_at", "-bk_host_id"]


class ProcessStatus(models.Model):
    class SourceType(object):
        DEFAULT = "default"
        SUBSCRIPTION = "subscription"
        DEBUG = "debug"

    SOURCE_TYPE_CHOICES = (
        (SourceType.DEFAULT, _("默认")),
        (SourceType.SUBSCRIPTION, _("订阅")),
        (SourceType.DEBUG, _("调试")),
    )
    GSE_AGENT_PROCESS_NAME = "gseagent"

    bk_host_id = models.IntegerField(_("主机ID"), db_index=True)
    name = models.CharField(_("进程名称"), max_length=45, default=GSE_AGENT_PROCESS_NAME, db_index=True)
    status = models.CharField(
        _("进程状态"), max_length=45, choices=const.PROC_STATE_CHOICES, default=const.ProcStateType.UNKNOWN, db_index=True
    )
    is_auto = models.CharField(
        _("是否自动启动"), max_length=45, choices=const.AUTO_STATE_CHOICES, default=const.AutoStateType.AUTO
    )
    version = models.CharField(_("进程版本"), max_length=45, blank=True, null=True, default="")
    proc_type = models.CharField(_("进程类型"), max_length=45, choices=const.PROC_CHOICES, default=const.ProcType.AGENT)

    configs = JSONField(_("配置文件"), default=list)
    listen_ip = models.CharField(_("监听IP"), max_length=45, null=True)
    listen_port = models.IntegerField(_("监听端口"), null=True)

    setup_path = models.TextField(_("二进制文件所在路径"), default="")
    log_path = models.TextField(_("日志路径"), default="")
    data_path = models.TextField(_("数据文件路径"), default="")
    pid_path = models.TextField(_("pid文件路径"), default="")

    group_id = models.CharField(_("插件组ID"), max_length=50, default="", db_index=True)
    source_type = models.CharField(_("来源类型"), max_length=128, default=SourceType.DEFAULT, choices=SOURCE_TYPE_CHOICES)
    source_id = models.CharField(_("来源ID"), max_length=128, default=None, null=True)

    retry_times = models.IntegerField("重试次数", default=0)

    @property
    def host(self):
        if not getattr(self, "_host", None):
            try:
                self._host = Host.objects.get(bk_host_id=self.bk_host_id)
            except Packages.DoesNotExist:
                raise Exception(_("获取主机失败: {}").format(self.bk_host_id))
        return self._host

    @property
    def package(self):
        if not getattr(self, "_package", None):
            os_type = self.host.os_type.lower()
            cpu_arch = Host.get_cpu_arch_by_os(os_type)
            try:
                self._package = Packages.objects.get(
                    project=self.name, version=self.version, os=os_type, cpu_arch=cpu_arch
                )
            except Packages.DoesNotExist:
                raise Exception(_("获取插件信息失败: {}-{}-{}").format(self.name, self.version, self.host.os_type))
        return self._package

    @property
    def host_info(self):
        """
        获取主机IP、云区域等信息
        """
        if not getattr(self, "_host_info", None):
            host = Host.objects.get(bk_host_id=self.bk_host_id)
            self._host_info = {
                "ip": host.inner_ip,
                "bk_cloud_id": host.bk_cloud_id,
                "bk_supplier_id": const.DEFAULT_SUPPLIER_ID,
            }
        return self._host_info

    class Meta:
        verbose_name = _("主机进程状态")
        verbose_name_plural = _("主机进程状态")


class AccessPoint(models.Model):
    name = models.CharField(_("接入点名称"), max_length=255)
    ap_type = models.CharField(_("接入点类型"), max_length=255, default="user")
    region_id = models.CharField(_("区域id"), max_length=255, default="")  # 从100开始自增
    city_id = models.CharField(_("城市id"), max_length=255, default="")
    btfileserver = JSONField(_("GSE BT文件服务器列表"))
    dataserver = JSONField(_("GSE 数据服务器列表"))
    taskserver = JSONField(_("GSE 任务服务器列表"))
    zk_hosts = JSONField(_("ZK服务器列表"))
    zk_account = models.CharField(_("ZK账号"), max_length=255, default="")
    zk_password = AESTextField(_("密码"), blank=True, null=True)
    package_inner_url = models.TextField(_("安装包内网地址"))
    package_outer_url = models.TextField(_("安装包外网地址"))
    nginx_path = models.TextField(_("Nginx路径"), blank=True, null=True)
    agent_config = JSONField(_("Agent配置信息"))
    status = models.CharField(_("接入点状态"), max_length=255, default="")
    description = models.TextField(_("接入点描述"))
    is_enabled = models.BooleanField(_("是否启用"), default=True)
    is_default = models.BooleanField(_("是否默认接入点，不可删除"), default=False)
    creator = JSONField(_("接入点创建者"), default=("admin",))
    port_config = JSONField(_("GSE端口配置"), default=dict)

    @staticmethod
    def test(params: dict):
        """
        接入点可用性测试
        :param params: Dict
        {
            "servers": [
                {
                    "inner_ip": "127.0.0.1",
                    "outer_ip": "127.0.0.2"
                }
            ],
            "package_inner_url": "http://127.0.0.1/download/",
            "package_outer_url": "http://127.0.0.2/download/"
        }
        :return:
        """

        def _check_ip(ip: str, _logs: list):
            try:
                subprocess.check_output(["ping", "-c", "1", ip, "-i", "1"])
            except subprocess.CalledProcessError as e:
                _logs.append(
                    {"log_level": "ERROR", "log": ugettext("Ping {ip} 失败, {output}").format(ip=ip, output=e.output)}
                )
            else:
                _logs.append({"log_level": "INFO", "log": ugettext("Ping {ip} 正常").format(ip=ip)})

        def _check_package_url(url: str, _logs: list):
            # TODO 检测方案待讨论确认
            download_url = f"{url}/setup_agent.sh"
            try:
                response = requests.get(download_url, timeout=2)
            except requests.RequestException:
                _logs.append(
                    {
                        "log_level": "ERROR",
                        "log": ugettext("{download_url} 检测下载失败，目标地址没有 setup_agent.sh 文件").format(
                            download_url=download_url
                        ),
                    }
                )
            else:
                if response.status_code != 200:
                    _logs.append(
                        {
                            "log_level": "ERROR",
                            "log": ugettext("{download_url} 检测下载失败").format(download_url=download_url),
                        }
                    )
                else:
                    _logs.append(
                        {
                            "log_level": "INFO",
                            "log": ugettext("{download_url} 检测下载成功").format(download_url=download_url),
                        }
                    )

        test_logs = []

        servers = params.get("btfileserver", []) + params.get("dataserver", []) + params.get("taskserver", [])

        with ThreadPoolExecutor(max_workers=settings.CONCURRENT_NUMBER) as ex:
            tasks = [ex.submit(_check_ip, server["inner_ip"], test_logs) for server in servers]
            tasks.append(ex.submit(_check_package_url, params["package_inner_url"], test_logs))
            tasks.append(ex.submit(_check_package_url, params["package_outer_url"], test_logs))
            as_completed(tasks)
        test_result = True
        for log in test_logs:
            if log["log_level"] == "ERROR":
                test_result = False

        return test_result, test_logs

    class Meta:
        verbose_name = _("接入点信息")
        verbose_name_plural = _("接入点信息")


class Cloud(models.Model):
    """云区域信息"""

    bk_cloud_id = models.IntegerField(primary_key=True)
    bk_cloud_name = models.CharField(max_length=45)
    isp = models.CharField(_("云服务商"), max_length=45, null=True, blank=True)
    ap_id = models.IntegerField(_("接入点ID"), null=True)
    creator = JSONField(_("云区域创建者"))

    is_visible = models.BooleanField(_("是否可见"), default=True)
    is_deleted = models.BooleanField(_("是否删除"), default=False)

    class Meta:
        verbose_name = _("云区域信息")
        verbose_name_plural = _("云区域信息")


class Job(models.Model):
    """任务信息"""

    created_by = models.CharField(_("操作人"), max_length=45, default="")
    job_type = models.CharField(
        _("作业类型"), max_length=45, choices=const.JOB_CHOICES, default=const.JobType.INSTALL_PROXY
    )
    subscription_id = models.IntegerField(_("订阅ID"), db_index=True)
    task_id_list = JSONField(_("任务ID列表"), default=list)
    start_time = models.DateTimeField(_("创建任务时间"), auto_now_add=True)
    end_time = models.DateTimeField(_("任务结束时间"), blank=True, null=True)
    status = models.CharField(
        _("任务状态"), max_length=45, choices=const.JobStatusType.get_choices(), default=const.JobStatusType.RUNNING
    )
    global_params = JSONField(_("全局运行参数"), blank=True, null=True)
    statistics = JSONField(_("任务统计信息"), blank=True, null=True, default=dict)
    bk_biz_scope = JSONField(_("业务范围"))
    error_hosts = JSONField(_("发生错误的主机"))

    class Meta:
        verbose_name = _("任务信息")
        verbose_name_plural = _("任务信息")
        ordering = ["-id"]


class JobTask(models.Model):
    """主机和任务关联表，存储任务详情及结果"""

    job_id = models.IntegerField(_("作业ID"), db_index=True)
    bk_host_id = models.IntegerField(_("主机ID"), db_index=True)
    instance_id = models.CharField(_("实例ID"), max_length=45, db_index=True)
    pipeline_id = models.CharField(_("Pipeline节点ID"), max_length=50, default="", blank=True, db_index=True)
    status = models.CharField(max_length=45, choices=const.STATUS_CHOICES, default=const.StatusType.QUEUE)
    current_step = models.CharField(_("当前步骤"), max_length=45, default="")
    create_time = models.DateTimeField(auto_now_add=True, db_index=True)
    update_time = models.DateTimeField(auto_now=True, db_index=True)
    end_time = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name = _("任务详情")
        verbose_name_plural = _("任务详情")


class Profile(models.Model):
    """个人资料补充信息"""

    bk_username = models.CharField(_("用户名"), max_length=45)
    favorite = JSONField(_("用户收藏信息,比如云区域等"))
    update_time = DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("个人资料")
        verbose_name_plural = _("个人资料")


class GsePluginDesc(models.Model):
    """
    插件信息表
    """

    # 插件名需要全局唯一，防止冲突
    name = models.CharField(_("插件名"), max_length=32, unique=True, db_index=True)
    description = models.TextField(_("插件描述"))
    scenario = models.TextField(_("使用场景"))
    description_en = models.TextField(_("英文插件描述"), null=True, blank=True)
    scenario_en = models.TextField(_("英文使用场景"), null=True, blank=True)
    category = models.CharField(_("所属范围"), max_length=32, choices=const.CATEGORY_CHOICES)
    launch_node = models.CharField(
        _("宿主节点类型要求"), max_length=32, choices=[("agent", "agent"), ("proxy", "proxy"), ("all", "all")], default="all"
    )

    config_file = models.CharField(_("配置文件名称"), max_length=128, null=True, blank=True)
    config_format = models.CharField(
        _("配置文件格式类型"), max_length=32, choices=const.CONFIG_FILE_FORMAT_CHOICES, default="json", null=True, blank=True
    )

    use_db = models.BooleanField(_("是否使用数据库"), default=0)
    auto_launch = models.BooleanField(_("是否在成功安装agent后自动拉起"), default=False)
    is_binary = models.BooleanField(_("是否二进制文件"), default=1)

    class Meta:
        verbose_name = _("插件信息表")
        verbose_name_plural = _("插件信息表")

    def __unicode__(self):
        return self.name

    @property
    def is_official(self):
        """
        是否为官方插件
        """
        return self.category == const.CategoryType.official

    @classmethod
    def get_auto_launch_plugins(cls):
        return cls.objects.filter(auto_launch=True)

    def get_package_by_os(self, os, pkg_name):
        package = Packages.objects.get(
            project=self.name,
            os=os,
            pkg_name=pkg_name,
            cpu_arch__in=[constants.CpuType.x86_64, constants.CpuType.powerpc],
        )
        return package

    def get_control_by_os(self, os):
        control = ProcControl.objects.filter(project=self.name, os=os).order_by("id").last()
        return control

    def get_packages(self, version=None, os=None, cpu_arch=None):
        """
        按照版本、操作系统、CPU架构查询插件包记录。
        如果没有版本，则使用最新版本。
        :param version: str 版本号
        :param os: str 操作系统
        :param cpu_arch: str CPU架构
        :return: list[Packages]
        """
        query_params = {"project": self.name}

        if os is not None:
            query_params["os"] = os
        if cpu_arch is not None:
            query_params["cpu_arch"] = cpu_arch
        if version is not None:
            query_params["version"] = version

        return Packages.objects.filter(**query_params)

    @classmethod
    def get_plugins(cls):
        plugin_list_cache = cache.get("plugin_list")
        if plugin_list_cache:
            return plugin_list_cache

        plugin_list = list(GsePluginDesc.objects.filter(category="official").values_list("name", flat=True))
        cache.set("plugin_list", plugin_list, 300)
        return plugin_list

    @classmethod
    def release_plugin(cls, md5_list, package_ids=None, query_params=None):
        """
        发布插件
        :param md5_list: list 各插件包的md5，用于校验
        :param package_ids: list 插件包的id，如果有这个参数，则query_params失效
        :param query_params: dict 使用插件
        :return:
        """
        query_params = copy.deepcopy(query_params)
        # 查询版本包，优先使用id查询插件包
        if package_ids:
            packages = Packages.objects.filter(id__in=package_ids)
        else:
            packages = cls(name=query_params.pop("name")).get_packages(**query_params)

        # 比对md5
        if "|".join(sorted(md5_list)) != "|".join(sorted([package.md5 for package in packages])):
            raise ValueError("md5 not match")

        # 修改发布状态
        packages.update(is_release_version=True)

        return packages


class Packages(models.Model):
    """
    插件更新包信息表
    """

    pkg_name = models.CharField(_("压缩包名"), max_length=128)
    version = models.CharField(_("版本号"), max_length=128)
    module = models.CharField(_("所属服务"), max_length=32)
    project = models.CharField(_("工程名"), max_length=32, db_index=True)
    pkg_size = models.IntegerField(_("包大小"))
    pkg_path = models.CharField(_("包路径"), max_length=128)
    md5 = models.CharField(_("md5值"), max_length=32)
    pkg_mtime = models.CharField(_("包更新时间"), max_length=48)
    pkg_ctime = models.CharField(_("包创建时间"), max_length=48)
    location = models.CharField(_("安装包链接"), max_length=512)
    os = models.CharField(
        _("系统类型"), max_length=32, choices=const.PLUGIN_OS_CHOICES, default=const.PluginOsType.linux, db_index=True
    )
    cpu_arch = models.CharField(
        _("CPU类型"), max_length=32, choices=const.CPU_CHOICES, default=const.CpuType.x86_64, db_index=True
    )

    is_release_version = models.BooleanField(_("是否已经发布版本"), default=True, db_index=True)
    # 由于创建记录时，文件可能仍然在传输过程中，因此需要标志位判断是否已经可用
    is_ready = models.BooleanField(_("插件是否可用"), default=True)

    @property
    def plugin_desc(self):
        if not hasattr(self, "_plugin_desc"):
            self._plugin_desc = GsePluginDesc.objects.get(name=self.project)
        return self._plugin_desc

    @property
    def proc_control(self):
        """
        获取进程控制信息
        """
        if not hasattr(self, "_proc_control"):
            self._proc_control = ProcControl.objects.get(plugin_package_id=self.id)
        return self._proc_control

    @classmethod
    @transaction.atomic
    def create_record(
        cls,
        dir_path,
        package_os,
        cpu_arch,
        is_external,
        is_release=True,
        is_template_load=False,
        is_template_overwrite=False,
    ):
        """
        给定一个插件的路径，分析路径下的project.yaml，生成压缩包到nginx(多台)目录下
        ！！！注意：该任务可能会导致长期的卡顿，请务必注意不要再wsgi等单线程环境中调用！！！
        :param dir_path: 需要进行打包的插件路径, 例如，plugin_a路径，路径下放置了插件各个文件
        :param package_os: 插件包支持的系统
        :param cpu_arch: 插件支持的CPU架构
        :param is_external: 是否第三方插件
        :param is_release: 是否发布的版本
        :param is_template_load: 是否需要读取插件包中的配置模板
        :param is_template_overwrite: 是否可以覆盖已经存在的配置模板
        :return: True | raise Exception
        """
        # 1. 判断是否存在project.yaml文件
        project_file_path = os.path.join(dir_path, "project.yaml")
        if not os.path.exists(project_file_path):
            logger.error("try to pack path->[%s] but is not [project.yaml] file under file path" % dir_path)
            raise ValueError(_("找不到 {} project.yaml文件，打包失败".format(dir_path)))

        # 2. 解析project.yaml文件(版本，插件名等信息)
        try:
            with open(project_file_path, "r", encoding="utf-8") as project_file:
                yaml_config = yaml.safe_load(project_file)

        except (IOError, yaml.YAMLError) as error:
            logger.error(
                "failed to parse or read project_yaml->[{}] for->[{}]".format(project_file_path, traceback.format_exc())
            )
            six.raise_from(error, error)

        try:
            package_name = yaml_config["name"]
            version = yaml_config["version"]
            control_info = yaml_config.get("control", {})

        except KeyError as error:
            logger.error(
                "failed to get key info from project.yaml->[%s] for->[%s] maybe config file error?"
                % (project_file_path, traceback.format_exc())
            )
            raise ValueError(_("配置文件{}信息缺失，请确认后重试, 缺失字段: {}".format(project_file_path, error)))

        # 判断之前是否已经有发布过的该插件版本
        exists_object_list = cls.objects.filter(project=package_name, version=version, os=package_os, cpu_arch=cpu_arch)
        if exists_object_list.filter(is_release_version=True).exists():
            logger.error(
                "project->[%s] version->[%s] os->[%s] cpu_arch->[%s] is release, no more operations is "
                "allowed." % (package_name, version, package_os, cpu_arch)
            )

        # 判断插件类型是否符合预期
        if yaml_config["category"] not in const.CATEGORY_TUPLE:
            logger.error(
                "project->[%s] version->[%s] update(or create) with category->[%s] which is not acceptable, "
                "nothing will do." % (package_name, version, yaml_config["category"])
            )
            raise ValueError(_("project.yaml中category配置异常，请确认后重试"))

        # 3. 创建新的插件包信息
        # 判断是否已经由插件描述信息，需要写入
        desc, created = GsePluginDesc.objects.update_or_create(
            name=package_name,
            defaults=dict(
                description=yaml_config.get("description", ""),
                scenario=yaml_config.get("scenario", ""),
                description_en=yaml_config.get("description_en", ""),
                scenario_en=yaml_config.get("scenario_en", ""),
                category=yaml_config["category"],
                launch_node=yaml_config.get("launch_node", "all"),
                config_file=yaml_config.get("config_file", ""),
                config_format=yaml_config.get("config_format", ""),
                use_db=bool(yaml_config.get("use_db", False)),
                auto_launch=bool(yaml_config.get("auto_launch", False)),
                is_binary=bool(yaml_config.get("is_binary", True)),
            ),
        )
        if created:
            logger.info("desc->[{}] for pack->[{}] is created".format(desc.id, package_name))

        # 写入插件包信息
        file_name = "{}-{}.tgz".format(package_name, version)
        if not exists_object_list.exists():
            # 如果之前未有未发布的插件包信息，需要新建
            record = cls.objects.create(
                pkg_name=file_name,
                version=version,
                module="gse_plugin",
                project=package_name,
                pkg_size=0,
                pkg_path="",
                md5="",
                pkg_mtime="",
                pkg_ctime="",
                location="",
                os=package_os,
                cpu_arch=cpu_arch,
                is_release_version=is_release,
                is_ready=False,
            )
        else:
            # 否则，更新已有的记录即可
            record = exists_object_list[0]

        path_info = env.get_gse_env_path(package_name, is_windows=(package_os == "windows"))
        try:
            proc_control = ProcControl.objects.get(plugin_package_id=record.id)

        except ProcControl.DoesNotExist:
            proc_control = ProcControl.objects.create(
                module="gse_plugin", project=package_name, plugin_package_id=record.id,
            )

        # 判断是否需要更新配置文件模板
        if is_template_load:
            config_templates = yaml_config.get("config_templates", [])
            for templates_info in config_templates:
                is_main_config = templates_info.get("is_main_config", False)
                source_path = templates_info["source_path"]

                template_file_path = os.path.join(dir_path, source_path)
                if not os.path.exists(template_file_path):
                    logger.error(
                        "project.yaml need to import file->[%s] but is not exists, nothing will do."
                        % templates_info["source_path"]
                    )
                    raise IOError(_("找不到需要导入的配置模板文件[%s]") % source_path)

                # 如果允许覆盖，那么则可以使用这个包中的内容覆盖已有模板
                if is_template_overwrite:

                    query_set = PluginConfigTemplate.objects.filter(
                        plugin_name=record.project,
                        plugin_version=templates_info["plugin_version"],
                        name=templates_info["name"],
                        version=templates_info["version"],
                        is_main=is_main_config,
                    )

                    if query_set.exists():
                        template = query_set[0]
                        template.delete()
                        logger.warning(
                            "template->[%s] version->[%s] is create for plugin->[%s] version->[%s] is delete for "
                            "overwrite." % (template.name, template.version, record.project, record.version)
                        )

                template = PluginConfigTemplate.objects.create(
                    plugin_name=record.project,
                    plugin_version=templates_info["plugin_version"],
                    name=templates_info["name"],
                    version=templates_info["version"],
                    format=templates_info["format"],
                    file_path=templates_info["file_path"],
                    content=open(template_file_path).read(),
                    is_release_version=is_release,
                    creator="system",
                    create_time=timezone.now(),
                    source_app_code="bk_nodeman",
                    is_main=is_main_config,
                )

                logger.info(
                    "template->[%s] version->[%s] is create for plugin->[%s] version->[%s] is add"
                    % (template.name, template.version, record.project, record.version)
                )

                # 由于文件已经进入到了数据库中，此时需要清理tpl文件
                os.remove(template_file_path)
                logger.info("template->[%s] now is delete for info has loaded into database." % template_file_path)

        # 更新信息
        proc_control.install_path = path_info["install_path"]
        proc_control.log_path = path_info["log_path"]
        proc_control.data_path = path_info["data_path"]
        proc_control.pid_path = path_info["pid_path"]
        proc_control.start_cmd = control_info.get("start", "")
        proc_control.stop_cmd = control_info.get("stop", "")
        proc_control.restart_cmd = control_info.get("restart", "")
        proc_control.reload_cmd = control_info.get("reload", "")
        proc_control.kill_cmd = control_info.get("kill", "")
        proc_control.version_cmd = control_info.get("version", "")
        proc_control.health_cmd = control_info.get("health_check", "")
        proc_control.debug_cmd = control_info.get("debug", "")
        proc_control.os = package_os

        # 更新插件二进制配置信息，如果不存在默认为空
        proc_control.process_name = yaml_config.get("process_name")

        # 更新是否需要托管
        proc_control.need_delegate = yaml_config.get("need_delegate", True)

        # 更新端口范围信息
        port_range = yaml_config.get("port_range", "")

        # 校验端口范围合法性
        port_range_list = ProcControl.parse_port_range(port_range)
        if port_range_list:
            proc_control.port_range = port_range

        proc_control.save()
        logger.info(
            "process control->[%s] for package->[%s] version->[%s] os->[%s] is created."
            % (proc_control.id, package_name, version, package_os)
        )

        # 4. 打包创建新的tar包
        file_name = "{}-{}.tgz".format(package_name, version)
        temp_file_path = "/tmp/{}-{}-{}-{}.tgz".format(package_name, version, package_os, cpu_arch)
        nginx_path = os.path.join(settings.NGINX_DOWNLOAD_PATH, record.os, record.cpu_arch, file_name)

        try:
            # 尝试创建 Nginx download path，已存在则忽略
            os.makedirs(os.path.dirname(nginx_path))
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise e

        with tarfile.open(temp_file_path, "w:gz") as tfile:
            tfile.add(
                dir_path,
                # 判断是否第三方插件的路径
                arcname="external_plugins/%s" % package_name if is_external else "plugins/",
            )
            logger.info(
                "package->[%s] version->[%s] now is pack to temp_path->[%s], ready to send to nginx."
                % (package_name, version, file_name)
            )

        # 4. 文件SCP转移到nginx路径下
        # 注意：此处需要依赖 NGINX_DOWNLOAD_PATH 挂载到 NFS
        shutil.copy(temp_file_path, nginx_path)

        # 5. 标记已经完成同步及其他信息
        record.is_ready = True
        record.pkg_ctime = record.pkg_mtime = str(timezone.now())
        record.pkg_size = os.path.getsize(temp_file_path)
        record.pkg_path = os.path.dirname(nginx_path)
        record.md5 = md5(temp_file_path)
        # 这里没有加上包名，是因为原本脚本(bkee/bkce)中就没有加上，为了防止已有逻辑异常，保持一致
        # 后面有哪位发现这里不适用了，可以一并修改
        record.location = "http://{}/download/{}/{}".format(os.getenv("LAN_IP"), package_os, cpu_arch)

        record.save()
        logger.info(
            "plugin->[{}] version->[{}] now is sync to nginx ready to use.".format(record.project, record.version)
        )

        return record

    @classmethod
    def export_plugins(cls, project, version):
        """
        导出指定插件
        !!! 注意：该方法会有打包及同步等高延迟的动作，请勿在同步环境(uwsgi)下使用 !!!
        :param project: 导出的插件名
        :param version: 导出的插件版本
        :return: {
            "file_path": ""/data/bkee/public/bk_nodeman/export/plugins-1.0.tgz
        } | raise Exception
        """

        # 1. 确认需要导出的文件
        # 注意：未完成发布及nginx准备的插件不可导出
        plugin_list = cls.objects.filter(project=project, version=version, is_ready=True, is_release_version=True)
        if not plugin_list.exists():
            logger.error(
                "user try to export plugin project->[%s] version->[%s] but is not exists, nothing will do."
                % (project, version)
            )
            raise ValueError(_("找不到可导出插件，请确认后重试"))

        # 临时的解压目录
        temp_path = "/tmp/%s" % uuid.uuid4().hex
        # 临时的压缩包路径
        temp_file_path = "/tmp/%s.tgz" % uuid.uuid4().hex

        # 2. 各个插件解压到指定的目录
        for plugin in plugin_list:
            plugin.unzip(temp_path)
            logger.info(
                "plugin->[{}] os->[{}] cpu->[{}] unzip success.".format(plugin.pkg_name, plugin.os, plugin.cpu_arch)
            )

        # 3. 解压的指定目录打包
        with tarfile.open(temp_file_path, "w:gz") as tar_file:
            # temp_path下的内容由于是从plugin处解压获得，所以应该已经符合external_plugins或者plugins的目录规范
            # 此处则不再指定
            tar_file.add(temp_path, ".")

        logger.debug(
            "export plugin->[%s] version->[%s] create temp_file->[%s] from path->[%s] success, "
            "ready to trans to nginx." % (project, version, temp_file_path, temp_path)
        )

        # 4. 同步到nginx指定目录
        file_name = "{}-{}-{}.tgz".format(project, version, md5(temp_file_path))

        if not os.path.exists(settings.EXPORT_PATH):
            os.makedirs(settings.EXPORT_PATH)

        download_file_path = os.path.join(settings.EXPORT_PATH, file_name)
        shutil.copy(temp_file_path, download_file_path)
        logger.info(
            "plugin->[{}] version->[{}] export file->[{}] is ready".format(project, version, download_file_path)
        )

        logger.info("plugin->[{}] version->[{}] export job success.".format(project, version))
        return {"file_path": download_file_path}

    def unzip(self, target_path):
        """
        将一个指定的插件解压到指定的目录下
        :param target_path: 指定的解压目录
        :return: True | raise Exception
        """

        file_path = os.path.join(self.pkg_path, self.pkg_name)

        # 1. 获取文件
        if not os.path.exists(file_path):
            logger.error(
                "try to unzip package->[{}] but file_path->[{}] is not exists, nothing will do.".format(
                    self.pkg_name, file_path
                )
            )
            raise ValueError(_("插件文件不存在，请联系管理员处理"))

        # 2. 解压到指定的目录
        with tarfile.open(file_path) as tar_file:

            file_members = tar_file.getmembers()

            # 判断获取需要解压到的目标位置
            if "external_plugins" in file_members[0].name:
                # 第三方插件的导出
                # 目标路径变更为：${target_path}/external_plugins_linux_x86/${project_name}/
                target_path = os.path.join(
                    target_path, "external_plugins_{}_{}".format(self.os, self.cpu_arch), self.project
                )
                logger.info(
                    "project->[%s] version->[%s] is external_plugins so set target_path->[%s]"
                    % (self.project, self.version, target_path)
                )
                plugin_root_path = "external_plugins/%s/" % self.project
                type_root_path = "external_plugins/"

            else:
                # 目标路径变更为：${target_path}/plugins_linux_x86/${project_name}/
                target_path = os.path.join(target_path, "plugins_{}_{}".format(self.os, self.cpu_arch), self.project)
                logger.info(
                    "project->[%s] version->[%s] is offical plugins so set target_path->[%s]"
                    % (self.project, self.version, target_path)
                )
                plugin_root_path = "plugins/%s/" % self.project
                type_root_path = "plugins/"

            if not os.path.exists(target_path):
                os.makedirs(target_path)
                logger.info("temp path->[{}] for package->[{}] is created".format(target_path, self.pkg_name))

            # 对所有的内容进行遍历，然后找到是文件的内容，解压到我们的目标路径上
            for member in file_members:

                # 如果是类型的层级文件夹，跳过
                if member.name == plugin_root_path[:-1] or member.name == type_root_path[:-1]:
                    logger.info(
                        "path->[{}] plugin_root_path->[{}] type_root_path->[{}] jump it".format(
                            member.name, plugin_root_path, type_root_path
                        )
                    )
                    continue

                # 解压时，只关注最底层的文件名及文件夹
                # 上层的external_plugins/project_name废弃
                file_name = member.name.replace(plugin_root_path, "")
                logger.info(
                    "path->[{}] is extract to->[{}] with replace_root->[{}]".format(
                        member.name, file_name, plugin_root_path
                    )
                )
                current_target_path = os.path.join(target_path, file_name)

                # 此处使用私有方法，是因为改名没有其他方式了
                # 如果其他大锅有更好的方案，欢迎修改。。。囧
                tar_file._extract_member(member, current_target_path)
                logger.info(
                    "project->[%s] version->[%s] file->[%s] is extract to->[%s]"
                    % (self.project, self.version, member.name, current_target_path)
                )

        logger.info(
            "package->[{}] os->[{}] cpu->[{}] unzip to path->[{}] success.".format(
                self.pkg_name, self.os, self.cpu_arch, target_path
            )
        )

        return True

    class Meta:
        verbose_name = _("模块/工程安装包信息表")
        verbose_name_plural = _("模块/工程安装包信息表")

    def __unicode__(self):
        return "{}-{}".format(self.module, self.project)


class ProcControl(models.Model):
    """
    插件更新包信息表
    """

    module = models.CharField(_("模块名"), max_length=32)
    project = models.CharField(_("工程名"), max_length=32)
    plugin_package_id = models.IntegerField(_("记录对应的插件包ID"), default=0)

    install_path = models.TextField(_("安装路径"))
    log_path = models.TextField(_("日志路径"))
    data_path = models.TextField(_("数据文件路径"))
    pid_path = models.TextField(_("pid文件路径"))

    start_cmd = models.TextField(_("启动命令"), default="", blank=True)
    stop_cmd = models.TextField(_("停止命令"), default="", blank=True)
    restart_cmd = models.TextField(_("重启命令"), default="", blank=True)
    reload_cmd = models.TextField(_("重载命令"), default="", blank=True)
    kill_cmd = models.TextField(_("kill命令"), default="", blank=True)
    version_cmd = models.TextField(_("进程版本查询命令"), default="", blank=True)
    health_cmd = models.TextField(_("进程健康检查命令"), default="", blank=True)
    debug_cmd = models.TextField(_("调试进程命令"), default="", blank=True)

    os = models.CharField(_("系统类型"), max_length=32, choices=const.PLUGIN_OS_CHOICES, default=const.PluginOsType.linux)
    process_name = models.CharField(_("实际二进制执行文件名"), max_length=128, null=True, default=None)
    port_range = models.TextField(_("插件允许使用的端口范围，格式 1,3,6-8,10-100"), null=True, blank=True, default="")
    need_delegate = models.BooleanField(_("是否需要托管"), default=True)

    @property
    def listen_port_required(self):
        """
        该插件是否需要监听端口
        """
        return bool(self.port_range)

    @classmethod
    def parse_port_num(cls, port_num):
        """
        检查端口号是否合法
        """
        if isinstance(port_num, six.string_types) and port_num.strip().isdigit():
            port_num = int(port_num)
        elif isinstance(port_num, int):
            pass
        else:
            raise ValueError(_("无法解析的端口号：%s") % port_num)

        if 1 <= port_num <= 65535:
            return port_num

        raise ValueError(_("不在合法范围内的端口号：%s") % port_num)

    @classmethod
    def parse_port_range(cls, port_range_str):
        """
        解析
        :param port_range_str: 端口范围字符串
        :return: 二元组列表，元组的两个元素分别代表开
        """

        # 为空直接返回
        if not port_range_str:
            return []

        port_range_list = []

        try:
            # 以逗号拆开
            range_str_list = port_range_str.split(",")
            for range_str in range_str_list:
                try:
                    # 先判断是不是单个数字
                    port_num = cls.parse_port_num(range_str)
                    # 如果是单个数字，则转化为区间并保存
                    port_range_list.append((port_num, port_num))
                except Exception:
                    # 如果不是单个数字，尝试识别为区间字符串
                    port_range_tuple = range_str.split("-")

                    # 尝试拆分为上界和下界
                    if len(port_range_tuple) != 2:
                        raise ValueError(_("不合法的端口范围定义格式：%s") % range_str)

                    # 对上界和下界分别进行解析
                    port_num_min, port_num_max = port_range_tuple
                    port_num_min = cls.parse_port_num(port_num_min)
                    port_num_max = cls.parse_port_num(port_num_max)

                    if port_num_min > port_num_max:
                        # 下界 > 上界 也是不合法的范围
                        raise ValueError(_("不合法的端口范围定义格式：%s") % range_str)
                    port_range_list.append((port_num_min, port_num_max))

        except Exception as e:
            raise ValueError(_("端口范围字符串解析失败：%s") % e)

        return port_range_list

    class Meta:
        verbose_name = _("模块/进程控制信息表")
        verbose_name_plural = _("模块/进程控制信息表")

    def __unicode__(self):
        return "{}-{}".format(self.module, self.project)


class UploadPackage(models.Model):
    """
    上传文件记录
    """

    file_name = models.CharField(_("上传包文件名"), max_length=64, db_index=True)
    module = models.CharField(_("模块名"), max_length=32)

    file_path = models.CharField(_("文件上传的路径名"), max_length=128)
    file_size = models.IntegerField(_("文件大小，单位Byte"))
    md5 = models.CharField(_("文件MD5"), max_length=32)

    upload_time = models.DateTimeField(_("文件上传时间"), auto_now_add=True)
    creator = models.CharField(_("上传用户名"), max_length=64)
    source_app_code = models.CharField(_("来源系统app code"), max_length=64)

    class Meta:
        verbose_name = _("文件包上传记录")
        verbose_name_plural = _("文件包上传记录表")

    def __unicode__(self):
        return "{}-{}".format(self.module, self.file_name)

    @classmethod
    def create_record(cls, module, file_path, md5, operator, source_app_code, file_name, is_file_copy=False):
        """
        创建一个新的上传记录
        :param module: 文件模块
        :param file_path: 文件在机器上的本地路径
        :param md5: 文件MD5
        :param operator: 操作者
        :param source_app_code: 上传来源APP_CODE
        :param file_name: 文件上传前的名字
        :param is_file_copy: 是否复制而非剪切文件，适应初始化内置插件需要使用
        :return: upload record
        """
        # 注意：MD5参数值将会直接使用，因为服务器上的MD5是由nginx协助计算，应该在views限制

        # 1. 判断文件是否已经存在
        if not os.path.exists(file_path):
            logger.warning(
                "user->[{}] try to create record for file->[{}] but is not exists.".format(operator, file_path)
            )
            raise ValueError(_("文件%s不存在，请确认后重试"), file_path)

        # 判断上传文件的路径是否已经存在
        if not os.path.exists(settings.UPLOAD_PATH):
            os.makedirs(settings.UPLOAD_PATH)
            logger.info("path->[{}] is not exists, and now is created by us.".format(settings.UPLOAD_PATH))

        # 3. 文件迁移到public
        new_file_path = os.path.join(settings.UPLOAD_PATH, file_name)

        try:
            if is_file_copy:
                shutil.copy(file_path, new_file_path)
            else:
                shutil.move(file_path, new_file_path)
        except IOError as error:
            logger.error(
                "failed to mv source_file->[%s] to targe_path->[%s] for->[%s]"
                % (file_path, new_file_path, traceback.format_exc())
            )
            six.raise_from(IOError(_("文件迁移失败，请联系管理员协助处理")), error)

        record = cls.objects.create(
            file_name=file_name,
            module=module,
            file_path=new_file_path,
            file_size=os.path.getsize(new_file_path),
            md5=md5,
            upload_time=timezone.now(),
            creator=operator,
            source_app_code=source_app_code,
        )
        logger.info(
            "new record for file->[%s] module->[%s] is added by operator->[%s] from system->[%s]."
            % (file_path, module, operator, source_app_code)
        )

        return record

    def create_package_records(self, is_release, is_template_load=False, is_template_overwrite=False):
        """
        拆解一个上传包并将里面的插件录入到package表中
        :param is_release: 是否正式发布
        :param is_template_load: 是否需要读取配置文件
        :param is_template_overwrite: 是否可以覆盖已经存在的配置文件
        :return: [package_object, ...]
        """
        # 1. 解压压缩文件
        package_result = []
        temp_path = "/tmp/%s" % uuid.uuid4().hex

        with tarfile.open(self.file_path) as tfile:
            # 检查是否存在可疑内容
            for file_info in tfile.getmembers():
                if file_info.name.startswith("/") or "../" in file_info.name:
                    logger.error(
                        "WTF? file->[{}] contains member->[{}] try to escape! We won't use it.".format(
                            self.file_path, file_info.name
                        )
                    )
                    raise ValueError(_("文件包含非法路径成员[%s]，请检查") % file_info.name)

            logger.info("file->[{}] extract to path->[{}] success.".format(self.file_path, temp_path))
            def is_within_directory(directory, target):
                
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
            
                prefix = os.path.commonprefix([abs_directory, abs_target])
                
                return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
            
                tar.extractall(path, members, numeric_owner=numeric_owner) 
                
            
            safe_extract(tfile, path=temp_path)

        # 2. 遍历第一层的内容，得知当前的操作系统和cpu架构信息
        with transaction.atomic():
            for first_path in os.listdir(temp_path):
                re_match = const.PACKAGE_PATH_RE.match(first_path)
                if re_match is None:
                    logger.info("path->[%s] is not match re, jump it." % first_path)
                    continue

                path_dict = re_match.groupdict()
                current_os = path_dict["os"]
                cpu_arch = path_dict["cpu_arch"]
                logger.info("path->[{}] is match for os->[{}] cpu->[{}]".format(first_path, current_os, cpu_arch))

                # 遍历第二层的内容，得知当前的插件名
                abs_first_path = os.path.join(temp_path, first_path)
                for second_path in os.listdir(abs_first_path):
                    # 注册新的内容，并触发同步
                    abs_path = os.path.join(abs_first_path, second_path)

                    if not os.path.isdir(abs_path):
                        logger.info("found file path->[%s] jump it" % abs_path)
                        continue

                    record = Packages.create_record(
                        dir_path=abs_path,
                        package_os=current_os,
                        cpu_arch=cpu_arch,
                        is_release=is_release,
                        is_external=path_dict["is_external"] is not None,
                        is_template_load=is_template_load,
                        is_template_overwrite=is_template_overwrite,
                    )

                    logger.info("package->[{}] now add record->[{}] success.".format(self.file_name, record.id))
                    package_result.append(record)

        # 3. 完成
        logger.info("now package->[%s] is all add done." % self.file_name)
        return package_result


class DownloadRecord(models.Model):
    """
    下载任务记录
    """

    # 任务状态枚举
    TASK_STATUS_READY = 0
    TASK_STATUS_DOING = 1
    TASK_STATUS_SUCCESS = 2
    TASK_STATUS_FAILED = 3

    TASK_STATUS_CHOICES = (
        (TASK_STATUS_READY, _("任务准备中")),
        (TASK_STATUS_DOING, _("任务进行中")),
        (TASK_STATUS_SUCCESS, _("任务已完成")),
        (TASK_STATUS_FAILED, _("任务失败")),
    )

    # 插件类型枚举
    CATEGORY_GSE_PLUGIN = "gse_plugin"

    CATEGORY_CHOICES = ((CATEGORY_GSE_PLUGIN, _("gse插件")),)

    CATEGORY_TASK_DICT = {CATEGORY_GSE_PLUGIN: Packages.export_plugins}

    category = models.CharField(_("下载文件类型"), max_length=32, choices=CATEGORY_CHOICES)
    query_params = models.CharField(_("下载查询参数"), max_length=256)

    file_path = models.CharField(_("打包后的文件路径"), max_length=256)
    task_status = models.IntegerField(_("任务状态"), default=0, choices=TASK_STATUS_CHOICES)
    error_message = models.TextField(_("任务错误信息"))

    creator = models.CharField(_("任务创建者"), max_length=64)
    create_time = models.DateTimeField(_("下载任务创建时间"), auto_now_add=True)
    finish_time = models.DateTimeField(_("任务完成时间"), auto_now=True)
    source_app_code = models.CharField(_("来源系统app code"), max_length=64)

    @property
    def is_finish(self):

        return self.task_status == self.TASK_STATUS_FAILED or self.task_status == self.TASK_STATUS_SUCCESS

    @property
    def is_failed(self):

        return self.task_status == self.TASK_STATUS_FAILED

    @property
    def download_params(self):
        """
        构造下载参数
        :return: id=xx&key=xxx
        """
        info_dict = {"job_id": self.id, "key": self.download_key}

        return six.moves.urllib.parse.urlencode(info_dict)

    @property
    def download_key(self):
        """
        下载验证key，防止恶意遍历下载文件
        :return:
        """
        # 由于下载的文件路径不会对外暴露，可以通过这个信息进行MD5校验
        md5 = hashlib.md5()
        md5.update(self.file_path.encode())

        return md5.hexdigest()

    @classmethod
    def create_record(cls, category, query_params, creator, source_app_code):
        """
        创建下载任务记录
        :param category: 下载文件类型
        :param query_params: 下载查询参数
        :param creator: 任务创建者
        :param source_app_code: 请求来源蓝鲸系统代号
        :return: download record
        """

        if category not in cls.CATEGORY_TASK_DICT:
            logger.error(
                "user->[%s] from source_app->[%s] request category->[%s] is not supported now, "
                "nothing will do." % (creator, source_app_code, category)
            )
            raise ValueError(_("请求下载类型[%s]暂不支持，请确认后重试") % category)

        record = cls.objects.create(
            category=category,
            query_params=json.dumps(query_params),
            file_path="",
            task_status=cls.TASK_STATUS_READY,
            error_message="",
            creator=creator,
            create_time=timezone.now(),
            source_app_code=source_app_code,
        )
        logger.info(
            "download record->[{}] is create from app->[{}] for category->[{}] query_params->[{}]".format(
                record.id, source_app_code, category, query_params
            )
        )

        return record

    def execute(self):
        """
        执行一个任务
        :return: True | raise Exception
        """
        task_status = self.TASK_STATUS_SUCCESS
        error_message = ""

        try:
            task_func = self.CATEGORY_TASK_DICT.get(self.category)
            self.task_status = self.TASK_STATUS_DOING
            self.save()

            # 直接利用请求的参数调用
            result = task_func(**json.loads(self.query_params))

            self.file_path = result["file_path"]

        except Exception as error:
            logger.error("failed to execute task->[{}] for->[{}]".format(self.id, traceback.format_exc()))

            task_status = self.TASK_STATUS_FAILED
            error_message = _("任务失败: %s") % error

            six.raise_from(error, error)

        finally:
            self.task_status = task_status
            self.error_message = error_message
            self.finish_time = timezone.now()
            self.save()
            logger.info(
                "task->[%s] is done with status->[%s] error_message->[%s]"
                % (self.id, self.task_status, self.error_message)
            )


class PluginConfigTemplate(models.Model):
    """
    插件配置文件模板
    """

    plugin_name = models.CharField(_("插件名"), max_length=32, db_index=True)
    plugin_version = models.CharField(_("版本号"), max_length=128, db_index=True)
    name = models.CharField(_("配置模板名"), max_length=128, db_index=True)
    version = models.CharField(_("配置模板版本"), max_length=128, db_index=True)
    is_main = models.BooleanField(_("是否主配置"), default=False, db_index=True)

    format = models.CharField(_("文件格式"), max_length=16)
    file_path = models.CharField(_("文件在该插件目录中相对路径"), max_length=128)
    content = models.TextField(_("配置内容"))
    is_release_version = models.BooleanField(_("是否已经发布版本"), db_index=True)

    creator = models.CharField(_("创建者"), max_length=64)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True)
    source_app_code = models.CharField(_("来源系统app code"), max_length=64)

    class Meta:
        verbose_name = _("插件配置文件模板")
        verbose_name_plural = _("插件配置文件模板表")
        # 唯一性限制
        unique_together = (
            # 对于同一个插件的同一个版本，同名配置文件只能存在一个
            ("plugin_name", "plugin_version", "name", "version", "is_main"),
        )

    def create_instance(self, data, creator=None, source_app_code=None):
        """
        返回 PluginConfigInstance 实例
        对于同一系统创建的实例，若data的MD5相同，则直接复用
        """
        json_data = json.dumps(data, sort_keys=True)
        hash_md5 = hashlib.md5(json_data.encode())
        data_md5 = hash_md5.hexdigest()

        # 创建配置文件实例
        instance, is_create = PluginConfigInstance.objects.update_or_create(
            plugin_config_template=self.id,
            source_app_code=source_app_code or self.source_app_code,
            data_md5=data_md5,
            defaults=dict(render_data=json_data, creator=creator or self.creator,),
        )

        return instance


class PluginConfigInstance(models.Model):
    """
    插件配置文件实例
    """

    plugin_config_template = models.IntegerField(_("对应实例记录ID"), db_index=True)
    render_data = models.TextField(_("渲染参数"))
    data_md5 = models.CharField(_("渲染参数MD5"), max_length=50, default="")
    creator = models.CharField(_("创建者"), max_length=64)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    source_app_code = models.CharField(_("来源系统app code"), max_length=64, db_index=True)

    @staticmethod
    def get_built_in_variables():
        """
        内置变量
        """
        return {}

    def render_config_template(self, extra_context=None):
        # 添加内置变量
        extra_context = extra_context or {}
        render_data = json.loads(self.render_data)

        # 使用模板引擎渲染用户参数
        render_data = nested_render_data(render_data, extra_context)

        # 先用 extra_context 去渲染 render_data 本身
        context = copy.deepcopy(extra_context)
        context.update(render_data)

        template = Template(self.template.content)
        template_content = template.render(context)
        return template_content

    @property
    def template(self):
        if not hasattr(self, "_template"):
            self._template = PluginConfigTemplate.objects.get(id=self.plugin_config_template)
        return self._template

    class Meta:
        verbose_name = _("插件配置文件实例")
        verbose_name_plural = _("插件配置文件实例表")


class SubscriptionStep(models.Model):
    """ 订阅步骤 """

    subscription_id = models.IntegerField(_("订阅ID"), db_index=True)
    index = models.IntegerField(_("顺序"), default=0)
    step_id = models.CharField(_("步骤ID"), max_length=64, db_index=True)
    type = models.CharField(_("步骤类型"), max_length=20)
    config = JSONField(_("配置"))
    params = JSONField(_("参数"))

    @property
    def subscription(self):
        if not hasattr(self, "_subscription"):
            self._subscription = Subscription.objects.get(id=self.subscription_id)
        return self._subscription

    @subscription.setter
    def subscription(self, value):
        if self.subscription_id != value.id:
            self.subscription_id = value.id
            self.save()
        self._subscription = value

    class Meta:
        ordering = ["index"]
        unique_together = (("subscription_id", "index"), ("subscription_id", "step_id"))


class Subscription(models.Model):
    """ 订阅 """

    class ObjectType(object):
        HOST = "HOST"
        SERVICE = "SERVICE"

    OBJECT_TYPE_CHOICES = (
        (ObjectType.HOST, _("主机")),
        (ObjectType.SERVICE, _("服务")),
    )

    class NodeType(object):
        TOPO = "TOPO"
        INSTANCE = "INSTANCE"

    NODE_TYPE_CHOICES = (
        (NodeType.TOPO, _("拓扑")),
        (NodeType.INSTANCE, _("实例")),
        (NodeType.INSTANCE, _("实例")),
    )
    name = models.CharField(_("任务名称"), max_length=64, null=True, blank=True)
    bk_biz_id = models.IntegerField(_("业务ID"), db_index=True, null=True)
    object_type = models.CharField(_("对象类型"), max_length=20, choices=OBJECT_TYPE_CHOICES, db_index=True)
    node_type = models.CharField(_("节点类型"), max_length=20, choices=NODE_TYPE_CHOICES, db_index=True)
    nodes = JSONField(_("节点"), default=list)
    target_hosts = JSONField(_("下发的目标机器"), default=None, null=True)
    from_system = models.CharField(_("所属系统"), max_length=30)
    update_time = models.DateTimeField(_("更新时间"), auto_now=True, db_index=True)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    creator = models.CharField(_("操作人"), max_length=64, db_index=True)
    enable = models.BooleanField(default=True, db_index=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    is_main = models.BooleanField(default=False, db_index=True)

    @property
    def steps(self):
        if not getattr(self, "_steps", None):
            self._steps = SubscriptionStep.objects.filter(subscription_id=self.id)
            for step in self._steps:
                # 设置 subscription 属性，减少查询次数
                step.subscription = self
        return self._steps

    @property
    def scope(self):
        need_register = False
        if self.node_type == self.NodeType.INSTANCE and self.object_type == self.ObjectType.HOST:
            # nodes 中的信息未注册到CMDB，需要注册
            for node in self.nodes:
                if "instance_info" in node:
                    need_register = True
        return {
            "bk_biz_id": self.bk_biz_id,
            "object_type": self.object_type,
            "node_type": self.node_type,
            "nodes": self.nodes,
            "need_register": need_register,
        }

    @classmethod
    def get_subscriptions(cls, ids, is_deleted=False):
        if is_deleted is None:
            subscriptions = cls.objects.filter(id__in=ids)
        else:
            subscriptions = cls.objects.filter(id__in=ids, is_deleted=is_deleted)
        subscription_steps = SubscriptionStep.objects.filter(
            subscription_id__in=[subscription.id for subscription in subscriptions]
        )
        subscription_step_dict = defaultdict(list)
        for step in subscription_steps:
            subscription_step_dict[step.subscription_id].append(step)

        for subscription in subscriptions:
            subscription._steps = subscription_step_dict[subscription.id]

        return subscriptions

    def is_running(self):
        """ 订阅下是否有运行中的任务 """
        from apps.backend.utils.pipeline_parser import check_running_records

        return check_running_records(SubscriptionInstanceRecord.objects.filter(subscription_id=self.id, is_latest=True))


class SubscriptionTask(models.Model):
    """ 订阅执行任务 """

    subscription_id = models.IntegerField(_("订阅ID"), db_index=True)
    scope = JSONField(_("执行范围"))
    actions = JSONField(_("不同step执行的动作名称。键值对"))
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    is_auto_trigger = models.BooleanField(_("是否为自动触发"), default=False)

    @property
    def subscription(self):
        if not hasattr(self, "_subscription"):
            self._subscription = Subscription.objects.get(id=self.subscription_id)
        return self._subscription

    @subscription.setter
    def subscription(self, value):
        if self.subscription_id != value.id:
            self.subscription_id = value.id
            self.save()
        self._subscription = value

    @property
    def instance_records(self):
        return SubscriptionInstanceRecord.objects.filter(task_id=self.id)

    class Meta:
        ordering = ["-create_time"]


class SubscriptionInstanceRecord(models.Model):
    """ 订阅任务的实例执行记录 """

    id = models.BigAutoField(primary_key=True)
    task_id = models.IntegerField(_("任务ID"), db_index=True)
    subscription_id = models.IntegerField(_("订阅ID"), db_index=True)
    instance_id = models.CharField(_("实例ID"), max_length=50, db_index=True)
    instance_info = JSONField(_("实例信息"))
    steps = JSONField(_("步骤信息"))
    pipeline_id = models.CharField(_("Pipeline ID"), max_length=50, default="", blank=True, db_index=True)
    update_time = models.DateTimeField(_("更新时间"), auto_now=True, db_index=True)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True, db_index=True)
    need_clean = models.BooleanField(_("是否需要清洗临时信息"), default=False)
    is_latest = models.BooleanField(_("是否为实例最新记录"), default=True, db_index=True)

    @property
    def subscription_task(self):
        if not hasattr(self, "_subscription_task"):
            self._subscription_task = SubscriptionTask.objects.get(id=self.task_id)
        return self._subscription_task

    @property
    def subscription(self):
        if not hasattr(self, "_subscription"):
            self._subscription = Subscription.objects.get(id=self.subscription_id)
        return self._subscription

    @subscription.setter
    def subscription(self, value):
        if self.subscription_id != value.id:
            self.subscription_id = value.id
            self.save()
        self._subscription = value

    def save_pipeline(self, pipeline_id, pipeline_tree):
        """
        设置pipeline属性
        """
        PipelineTree.objects.update_or_create(id=pipeline_id, defaults={"tree": pipeline_tree})
        self.pipeline_id = pipeline_id
        self.save()

    def run_pipeline(self):
        pipeline_tree = PipelineTree.objects.get(id=self.pipeline_id)
        pipeline_tree.run()

    def init_steps(self):
        """
        初始化步骤数据
        """
        step_data = [
            {"id": step.step_id, "type": step.type, "pipeline_id": "", "action": None, "extra_info": {}}
            for step in self.subscription.steps
        ]
        self.steps = step_data

    def get_all_step_data(self):
        if not self.steps:
            self.init_steps()

        return self.steps

    def get_step_data(self, step_id):
        """
        根据 step_id 获取步骤数据
        """
        if not self.steps:
            self.init_steps()

        for step in self.steps:
            if step["id"] == step_id:
                return step
        raise KeyError(_("步骤ID [{}] 在该订阅配置中不存在").format(step_id))

    def set_step_data(self, step_id, data):
        """
        根据 step_id 设置步骤数据
        """
        if not self.steps:
            self.init_steps()

        # 不允许修改 id 和 type
        data.pop("id", None)
        data.pop("type", None)

        steps = copy.deepcopy(self.steps)

        for step in steps:
            if step["id"] != step_id:
                continue

            # 更新步骤数据，保存
            step.update(data)
            self.steps = steps
            return

        raise KeyError(_("步骤ID [{}] 在该订阅配置中不存在").format(step_id))

    def simple_instance_info(self):
        instance_info = self.instance_info
        return {
            "host": {
                key: instance_info["host"].get(key)
                for key in instance_info["host"]
                if key
                in [
                    "bk_host_innerip",
                    "bk_cloud_id",
                    "bk_supplier_account",
                    "bk_host_name",
                    "bk_host_id",
                    "bk_biz_id",
                    "bk_biz_name",
                    "bk_cloud_name",
                ]
            },
            "service": {
                key: instance_info["service"].get(key)
                for key in (instance_info.get("service") or {})
                if key in ["id", "name", "bk_module_id", "bk_host_id"]
            },
        }


class CmdbEventRecord(models.Model):
    """ 记录CMDB事件回调 """

    bk_biz_id = models.IntegerField(_("订阅ID"), db_index=True)
    subscription_id = models.CharField(_("实例ID"), max_length=50, db_index=True)
    event_type = models.CharField(_("事件类型"), max_length=20)
    action = models.CharField(_("动作"), max_length=20)
    obj_type = models.CharField(_("对象类型"), max_length=32)
    data = JSONField(_("实例信息"))
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True)


class PipelineTree(models.Model):
    """
    记录Pipeline树 ID 与 拓扑的对应信息
    """

    id = models.CharField(_("PipelineID"), primary_key=True, max_length=32)
    tree = LazyJSONField(_("Pipeline拓扑树"))

    def run(self, priority=None):
        # 根据流程描述结构创建流程对象
        parser = PipelineParser(pipeline_tree=self.tree)
        pipeline = parser.parse()
        if priority is not None:
            action_result = task_service.run_pipeline(pipeline, priority=priority)
        else:
            action_result = task_service.run_pipeline(pipeline)

        if not action_result.result:
            raise PipelineExecuteFailed({"msg": action_result.message})


class ResourceWatchEvent(models.Model):
    """
    资源监听事件
    """

    EVENT_TYPE_CHOICE = (
        ("update", "update"),
        ("create", "create"),
        ("delete", "delete"),
    )

    bk_cursor = models.CharField(_("游标"), max_length=64, primary_key=True)
    bk_event_type = models.CharField(_("事件类型"), max_length=32, choices=EVENT_TYPE_CHOICE)
    bk_resource = models.CharField(_("资源"), max_length=32)
    bk_detail = JSONField(_("事件详情"), default=dict)
    create_time = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        verbose_name = _("资源监听事件")
        verbose_name_plural = _("资源监听事件")
