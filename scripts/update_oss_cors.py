# scripts/update_oss_cors.py
"""桶 CORS 合并追加 PUT 规则（导入直传，ADR-024）。幂等，不动现有规则。

用法（部署机 /opt/salary 下，env 已含 OSS_*）：
    docker exec openship-salary-calculation-backend python scripts/update_oss_cors.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3
from botocore.config import Config

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

try:
    rules = client.get_bucket_cors(Bucket=bucket)["CORSRules"]
except client.exceptions.NoSuchCORSConfiguration:
    rules = []

if any("PUT" in r.get("AllowedMethods", []) for r in rules):
    print("PUT 规则已存在，跳过")
else:
    rules.append({
        "AllowedMethods": ["PUT"],
        "AllowedOrigins": ["*"],
        "AllowedHeaders": ["*"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3000,
    })
    client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": rules})
    print(f"已向桶 {bucket} 追加 PUT CORS 规则（现有 {len(rules) - 1} 条保留）")
