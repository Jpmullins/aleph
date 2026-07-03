"""Unit tests for the AssetStore backends + factory (WP-1)."""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from aleph_rks.asset_store import (
    AssetStoreError,
    FsAssetStore,
    S3AssetStore,
    create_asset_store,
)


def test_fs_put_get_roundtrip(tmp_path):
    store = FsAssetStore(tmp_path)
    data = b"hello aleph"
    stored = store.put_source_asset(
        project_id=uuid4(),
        source_id=uuid4(),
        data=data,
        mime_type="text/plain",
        extension="txt",
    )
    assert stored.storage_uri.startswith("fs://projects/")
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert stored.size_bytes == len(data)
    assert store.get(stored.storage_uri, expected_sha256=stored.sha256) == data


def test_fs_stream_yields_all_bytes_in_chunks(tmp_path):
    store = FsAssetStore(tmp_path)
    data = b"x" * 1000
    stored = store.put_bytes(key="projects/p/blob.bin", data=data, mime_type="app/x")
    chunks = list(store.stream(stored.storage_uri, chunk_size=64))
    assert b"".join(chunks) == data
    assert all(len(c) <= 64 for c in chunks)


def test_fs_sha_mismatch_raises(tmp_path):
    store = FsAssetStore(tmp_path)
    stored = store.put_bytes(key="projects/p/a.txt", data=b"aaa", mime_type="text/plain")
    with pytest.raises(AssetStoreError, match="sha256 mismatch"):
        store.get(stored.storage_uri, expected_sha256="0" * 64)


def test_fs_normalized_markdown_roundtrip(tmp_path):
    store = FsAssetStore(tmp_path)
    uri = store.put_normalized_markdown(
        project_id=uuid4(), source_id=uuid4(), version_no=1, markdown="# hi"
    )
    assert uri.endswith("/1.md")
    assert store.get(uri) == b"# hi"


def test_fs_rejects_foreign_scheme(tmp_path):
    store = FsAssetStore(tmp_path)
    with pytest.raises(AssetStoreError, match="backend mismatch"):
        store.get("s3://bucket/key")
    with pytest.raises(AssetStoreError, match="backend mismatch"):
        list(store.stream("s3://bucket/key"))


def test_fs_rejects_path_traversal(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    store = FsAssetStore(tmp_path / "root")
    with pytest.raises(AssetStoreError, match="escapes"):
        store.get("fs://../outside.txt")


def test_fs_rejects_traversal_on_put_and_stream(tmp_path):
    store = FsAssetStore(tmp_path / "root")
    with pytest.raises(AssetStoreError, match="escapes"):
        store.put_bytes(key="../evil.bin", data=b"x", mime_type="app/x")
    with pytest.raises(AssetStoreError, match="escapes"):
        list(store.stream("fs://../evil.bin"))
    with pytest.raises(AssetStoreError, match="escapes"):
        store.get("fs:///etc/hostname")


def test_fs_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    root = tmp_path / "root"
    store = FsAssetStore(root)
    (root / "link").symlink_to(outside)
    with pytest.raises(AssetStoreError, match="escapes"):
        store.get("fs://link/secret.txt")


def test_fs_unwritable_dest_raises_asset_store_error(tmp_path):
    store = FsAssetStore(tmp_path)
    blocked = tmp_path / "projects"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        with pytest.raises(AssetStoreError, match="write failed"):
            store.put_bytes(key="projects/p/a.bin", data=b"x", mime_type="app/x")
    finally:
        blocked.chmod(0o755)


def test_fs_missing_file_raises(tmp_path):
    store = FsAssetStore(tmp_path)
    with pytest.raises(AssetStoreError, match="read failed"):
        store.get("fs://projects/p/nope.bin")


def test_fs_stream_missing_file_raises_before_iteration(tmp_path):
    # stream() must fail at call time, not first-chunk time: the streaming
    # route sends 200 + headers before iterating, so a lazy failure would
    # reach the browser as a truncated 200 instead of a 404.
    store = FsAssetStore(tmp_path)
    with pytest.raises(AssetStoreError, match="read failed"):
        store.stream("fs://projects/p/nope.bin")


def test_source_key_sanitizes_uploader_extension(tmp_path):
    store = FsAssetStore(tmp_path)
    stored = store.put_source_asset(
        project_id=uuid4(),
        source_id=uuid4(),
        data=b"x",
        mime_type="application/octet-stream",
        extension="../../evil/path.sh",
    )
    # Only the alphanumerics survive; no separators reach the key.
    assert stored.storage_uri.endswith(".evilpathsh")
    assert "/../" not in stored.storage_uri


def test_s3_rejects_foreign_scheme():
    # _parse is static — no client needed to verify the scheme guard.
    with pytest.raises(AssetStoreError, match="backend mismatch"):
        S3AssetStore._parse("fs://projects/p/a.txt")  # pyright: ignore[reportPrivateUsage]


def test_factory_defaults_to_fs(tmp_path):
    store = create_asset_store(root=tmp_path)
    assert isinstance(store, FsAssetStore)


def test_factory_no_args_returns_fs_store(tmp_path, monkeypatch):
    # The spec's literal claim: create_asset_store() with no args is an
    # FsAssetStore (rooted at ./data/assets — chdir so the side-effect
    # .tmp mkdir lands in the sandbox).
    monkeypatch.chdir(tmp_path)
    store = create_asset_store()
    assert isinstance(store, FsAssetStore)
    assert (tmp_path / "data" / "assets" / ".tmp").is_dir()


def test_factory_fs_explicit(tmp_path):
    store = create_asset_store(backend="fs", root=tmp_path)
    assert isinstance(store, FsAssetStore)


def test_factory_s3_missing_config_fails_fast():
    with pytest.raises(AssetStoreError, match="ALEPH_S3_ENDPOINT"):
        create_asset_store(backend="s3", bucket="b")
