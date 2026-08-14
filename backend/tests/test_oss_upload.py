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
    # 两种合法扩展名都贯穿到 key 尾部
    for ext in (".xlsx", ".xls"):
        key = oss_upload.upload_key("2026-07", "sales", ext)
        assert key.startswith("salary/uploads/2026-07/sales-")
        assert key.endswith(ext)
        assert oss_upload.key_matches(key, "2026-07", "sales") is True
        # key 与路径参数不一致 / 非本服务签发 → 拒绝
        assert oss_upload.key_matches(key, "2026-06", "sales") is False
        assert oss_upload.key_matches(key, "2026-07", "gifts") is False
    assert oss_upload.key_matches("salary/v3/2026-06.xlsx", "2026-07", "sales") is False


def test_upload_key_rejects_bad_ext(oss_env):
    from backend.app.services import oss_upload
    for ext in (".xlsm", ".csv", ".XLSX", "xlsx", ""):
        with pytest.raises(ValueError):
            oss_upload.upload_key("2026-07", "sales", ext)


def test_key_matches_rejects_bad_tail(oss_env):
    from backend.app.services import oss_upload
    hex32 = "0" * 32
    # 非 xlsx/xls 后缀 → 拒绝
    assert oss_upload.key_matches(f"salary/uploads/2026-07/sales-{hex32}.xlsm", "2026-07", "sales") is False
    assert oss_upload.key_matches(f"salary/uploads/2026-07/sales-{hex32}.csv", "2026-07", "sales") is False
    # 后缀大小写敏感（key 由后端小写签发，大写视为非法）
    assert oss_upload.key_matches(f"salary/uploads/2026-07/sales-{hex32}.XLSX", "2026-07", "sales") is False
    # 双后缀 / 尾巴多字符 → 拒绝
    assert oss_upload.key_matches(f"salary/uploads/2026-07/sales-a{hex32}.xlsx", "2026-07", "sales") is False
    assert oss_upload.key_matches(f"salary/uploads/2026-07/sales-{hex32}.xlsx/", "2026-07", "sales") is False


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


def test_fetch_to_file_atomic_keeps_old_file(oss_env, tmp_path, monkeypatch):
    """中断不破坏既有存档：写一半失败时目标文件保持原样，且不留 .part。"""
    from backend.app.services import oss_upload

    class _Body:
        def iter_chunks(self, size):
            yield b"new data partial"
            raise RuntimeError("传输中断")

    class _FakeUpload:
        def get_object(self, Bucket, Key):
            return {"Body": _Body()}

        def delete_object(self, Bucket, Key):
            pass

    monkeypatch.setattr(oss_upload, "_clients", lambda: (_FakeUpload(), None))
    target = tmp_path / "archive.xlsx"
    target.write_bytes(b"old archive")
    with pytest.raises(RuntimeError):
        oss_upload.fetch_to_file("salary/uploads/k.xlsx", str(target))
    assert target.read_bytes() == b"old archive"  # 旧存档未被覆写
    assert list(tmp_path.iterdir()) == [target]   # .part 已清理
