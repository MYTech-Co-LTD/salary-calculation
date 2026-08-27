# scripts/update_oss_cors.py
"""桶 CORS 合并追加 PUT 规则 + uploads/ 前缀 1 天过期 lifecycle（导入直传，ADR-024）。

幂等：均不动现有规则。用法（部署机 /opt/salary 下，env 已含 OSS_*）：
    docker exec openship-salary-calculation-backend python scripts/update_oss_cors.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64
import hashlib

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from backend.app.services.oss_export import _env

bucket = _env("OSS_BUCKET")
client = boto3.client(
    "s3",
    endpoint_url=_env("OSS_ENDPOINT_PUBLIC", "https://xinan1.zos.ctyun.cn"),
    aws_access_key_id=_env("OSS_ACCESS_KEY"),
    aws_secret_access_key=_env("OSS_SECRET_KEY"),
    region_name=_env("OSS_REGION", "xinan1"),
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)


def _inject_content_md5(request, **kwargs):
    """新版 botocore 默认发 x-amz-checksum-crc32，ZOS 只认 Content-MD5——按最终请求体补算注入。"""
    if not request.headers.get("Content-MD5") and request.body:
        request.headers["Content-MD5"] = base64.b64encode(
            hashlib.md5(request.body).digest()).decode()


client.meta.events.register("before-sign.s3.PutBucketLifecycleConfiguration", _inject_content_md5)

# ⚠ ZOS 预检“首条规则短路”坑（2026-08-27 生产踩坑）：多条同 Origin(*) 规则时，浏览器
# OPTIONS 预检按 Origin 命中第一条（GET/HEAD）后只在该条方法集内校验，即使另一条
# 单独含 PUT 也返回 403 "The request method are not whitelisted" 且无 CORS 响应头 →
# 浏览器拦截 XHR 直传。必须把 GET/HEAD/PUT 合并为单条规则。
MERGED_RULE = [{
    "AllowedMethods": ["GET", "HEAD", "PUT"],
    "AllowedOrigins": ["*"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000,
}]

try:
    rules = client.get_bucket_cors(Bucket=bucket)["CORSRules"]
except ClientError as e:  # ZOS 未建模 NoSuch*Configuration 异常类，只能按错误码判断
    if "NoSuch" not in str(e):
        raise
    rules = []

if len(rules) == 1 and set(rules[0].get("AllowedMethods", [])) == {"GET", "HEAD", "PUT"}:
    print("CORS 已是合并单条规则，跳过")
else:
    client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": MERGED_RULE})
    print(f"已将桶 {bucket} 的 CORS 合并为单条 GET/HEAD/PUT 规则（原 {len(rules)} 条：{rules}）")

# —— lifecycle：uploads/ 中转对象 1 天过期兜底（正常路径拉取即删，防异常残留堆积）——
LIFECYCLE_ID = "uploads-expire-1d"
uploads_prefix = f"{_env('OSS_PREFIX', 'salary/')}uploads/"


def _rule_prefix(rule):
    """兼容新旧两种 Filter 形态取前缀；无前缀规则返回 None。"""
    f = rule.get("Filter")
    if isinstance(f, dict) and "Prefix" in f:
        return f["Prefix"]
    return rule.get("Prefix")


try:
    lc_rules = client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
except ClientError as e:  # ZOS 未建模 NoSuchLifecycleConfiguration，按错误码判断
    if "NoSuch" not in str(e):
        raise
    lc_rules = []

if any(r.get("ID") == LIFECYCLE_ID or _rule_prefix(r) == uploads_prefix for r in lc_rules):
    print(f"lifecycle 规则 {LIFECYCLE_ID}（{uploads_prefix} 1 天过期）已存在，跳过")
else:
    lc_rules.append({
        "ID": LIFECYCLE_ID,
        "Filter": {"Prefix": uploads_prefix},
        "Status": "Enabled",
        "Expiration": {"Days": 1},
    })
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket, LifecycleConfiguration={"Rules": lc_rules})
    print(f"已向桶 {bucket} 追加 lifecycle 规则 {LIFECYCLE_ID}：{uploads_prefix} 前缀 1 天过期")
