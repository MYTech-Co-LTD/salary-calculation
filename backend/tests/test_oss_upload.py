# backend/tests/test_oss_upload.py
"""OSS 上传中转服务（ADR-024 Part C）：预签名 PUT + 内网拉取。"""
import pytest


@pytest.fixture
def oss_env(monkeypatch):
    monkeypatch.setenv("OSS_BUCKET", "test-bucket")
    monkeypatch.setenv("OSS_ACCESS_KEY", "ak")
    monkeypatch.setenv("OSS_SECRET_KEY", "sk")
    monkeypatch.setenv("OSS_ENDPOINT_INTERNAL", "http://internal.example")
    monkeypatch.setenv("OSS_ENDPOINT_PUBLIC", "https://pub.example")


def test_is_configured(oss_env):
    from backend.app.services import oss_upload
    assert oss_upload.is_configured() is True


def test_is_configured_false_without_env(monkeypatch):
    from backend.app.services import oss_upload
    for k in ("OSS_BUCKET", "OSS_ACCESS_KEY", "OSS_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert oss_upload.is_configured() is False


def test_upload_key_and_match(oss_env):
    from backend.app.services import oss_upload
    key = oss_upload.upload_key("2026-07", "sales")
    assert key.startswith("salary/uploads/2026-07/sales-")
    assert key.endswith(".xlsx")
    assert oss_upload.key_matches(key, "2026-07", "sales") is True
    # key 与路径参数不一致 / 非本服务签发 → 拒绝
    assert oss_upload.key_matches(key, "2026-06", "sales") is False
    assert oss_upload.key_matches(key, "2026-07", "gifts") is False
    assert oss_upload.key_matches("salary/v3/2026-06.xlsx", "2026-07", "sales") is False


def test_presign_put_url_shape(oss_env):
    from backend.app.services import oss_upload
    url = oss_upload.presign_put("salary/uploads/2026-07/sales-abc.xlsx")
    # 公网 virtual-host：bucket 拼进域名，path 是 key
    assert url.startswith("https://test-bucket.pub.example/")
    assert "salary/uploads/2026-07/sales-abc.xlsx" in url
    assert "X-Amz-Signature" in url


def test_fetch_to_file_writes_and_deletes(oss_env, tmp_path, monkeypatch):
    from backend.app.services import oss_upload

    deleted = []

    class _Body:
        def iter_chunks(self, size):
            yield b"hello "
            yield b"xlsx"

    class _FakeUpload:
        def get_object(self, Bucket, Key):
            assert Bucket == "test-bucket" and Key == "salary/uploads/k.xlsx"
            return {"Body": _Body()}

        def delete_object(self, Bucket, Key):
            deleted.append(Key)

    monkeypatch.setattr(oss_upload, "_clients", lambda: (_FakeUpload(), None))
    p = tmp_path / "out.xlsx"
    oss_upload.fetch_to_file("salary/uploads/k.xlsx", str(p))
    assert p.read_bytes() == b"hello xlsx"
    assert deleted == ["salary/uploads/k.xlsx"]  # 用完即删


def test_fetch_to_file_deletes_even_on_failure(oss_env, tmp_path, monkeypatch):
    from backend.app.services import oss_upload

    deleted = []

    class _BrokenUpload:
        def get_object(self, Bucket, Key):
            raise RuntimeError("网络断")

        def delete_object(self, Bucket, Key):
            deleted.append(Key)

    monkeypatch.setattr(oss_upload, "_clients", lambda: (_BrokenUpload(), None))
    with pytest.raises(RuntimeError):
        oss_upload.fetch_to_file("salary/uploads/k.xlsx", str(tmp_path / "o.xlsx"))
    assert deleted == ["salary/uploads/k.xlsx"]
