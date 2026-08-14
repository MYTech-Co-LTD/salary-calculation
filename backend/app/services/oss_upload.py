# backend/app/services/oss_upload.py
"""导入文件经 OBS 中转上传（ADR-024 Part C）。

与导出下载（ADR-022 oss_export）对称：浏览器拿公网预签名 URL 直传 PUT，
后端从云内网拉回本地 uploads 目录，拉取后即删对象（中转用完即弃）。
"""
import re
from uuid import uuid4

from backend.app.services.oss_export import _clients, _env, is_configured  # noqa: F401 (re-export)

__all__ = ["is_configured", "upload_key", "key_matches", "presign_put", "fetch_to_file"]


def upload_key(month: str, kind: str) -> str:
    """生成一次性上传 key：{prefix}uploads/{month}/{kind}-{uuid}.xlsx"""
    return f"{_env('OSS_PREFIX', 'salary/')}uploads/{month}/{kind}-{uuid4().hex}.xlsx"


def key_matches(key: str, month: str, kind: str) -> bool:
    """校验 import-oss 收到的 key 确为本服务签发的当月当类型上传 key。

    防登录用户借端点拉桶内任意对象（如导出文件）。"""
    prefix = f"{_env('OSS_PREFIX', 'salary/')}uploads/{month}/{kind}-"
    if not key.startswith(prefix):
        return False
    return bool(re.fullmatch(r"[0-9a-f]{32}\.xlsx", key[len(prefix):]))


def presign_put(key: str, expires: int = 600) -> str:
    """公网 virtual-host 预签名 PUT URL（默认 10 分钟有效）。"""
    _, presign = _clients()
    return presign.generate_presigned_url(
        "put_object", Params={"Bucket": _env("OSS_BUCKET"), "Key": key}, ExpiresIn=expires)


def fetch_to_file(key: str, local_path: str) -> None:
    """内网拉取对象写本地文件。finally 总删对象：中转用完即弃，
    失败也不留垃圾（重试 = 前端重新走 ticket 整段上传）。"""
    upload, _ = _clients()
    bucket = _env("OSS_BUCKET")
    try:
        resp = upload.get_object(Bucket=bucket, Key=key)
        with open(local_path, "wb") as out:
            for chunk in resp["Body"].iter_chunks(1024 * 1024):
                out.write(chunk)
    finally:
        try:
            upload.delete_object(Bucket=bucket, Key=key)
        except Exception:
            pass  # 删除失败不掩盖主流程异常
