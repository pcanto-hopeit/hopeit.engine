from datetime import datetime, timezone
import io
from typing import List, Optional

import os
from pathlib import Path

import aiofiles

import hopeit.fs_storage as fs_module
from hopeit.dataobjects import dataclass, dataobject
from hopeit.fs_storage import FileStorage, FileStorageSettings, ItemLocator
import pytest


@dataobject(event_ts="ts")
@dataclass
class FsMockData:
    test: str
    ts: Optional[datetime] = None


@dataobject
@dataclass
class FsMockDataNoTs:
    test: str


payload_str = """{"test":"test_fs"}"""
test_fs = FsMockData(test="test_fs")

payload_str_with_ts = """{"test":"test_fs", "ts":"2022-03-01T00:00:00+00:00"}"""
test_fs_with_ts = FsMockData(test="test_fs", ts=datetime(2022, 3, 1, tzinfo=timezone.utc))
test_fs_without_ts = FsMockDataNoTs(test="test_fs")

binary_file = b"Binary file content"


async def save_and_load_file():
    key = "VALIDFILE"
    fs = FileStorage(path=f"/tmp/{key}/")
    path = await fs.store(key, test_fs)
    assert path == f"/tmp/{key}/{key}.json"
    loaded = await fs.get(key, datatype=FsMockData)
    assert loaded == test_fs
    assert type(loaded) is FsMockData


async def delete_files():
    key1 = "VALIDFILE1"
    key2 = "VALIDFILE2"
    key3 = "BINARY.FILE"
    fs = FileStorage(path="/tmp/DELETE/")
    assert await fs.store(key1, test_fs) is not None
    assert await fs.store(key2, test_fs) is not None
    assert await fs.store_file(key3, io.BytesIO(binary_file)) is not None
    assert await fs.get(key1, datatype=FsMockData) == test_fs
    assert await fs.get(key2, datatype=FsMockData) == test_fs
    await fs.delete(key1, key2)
    await fs.delete_files(key3)
    assert await fs.get(key1, datatype=FsMockData) is None
    assert await fs.get(key2, datatype=FsMockData) is None
    assert await fs.get_file(key3) is None


async def delete_files_in_partition():
    key1 = "VALIDFILE1"
    key2 = "VALIDFILE2"
    fs = FileStorage(path="/tmp/DELETE/", partition_dateformat="%Y/%m/%d")
    partition_key = datetime.now(tz=timezone.utc).strftime("%Y/%m/%d")
    assert await fs.store(key1, test_fs) is not None
    assert await fs.store(key2, test_fs) is not None
    assert await fs.get(key1, datatype=FsMockData, partition_key=partition_key) == test_fs
    assert await fs.get(key2, datatype=FsMockData, partition_key=partition_key) == test_fs
    await fs.delete(key1, key2, partition_key=partition_key)
    assert await fs.get(key1, datatype=FsMockData, partition_key=partition_key) is None
    assert await fs.get(key2, datatype=FsMockData, partition_key=partition_key) is None


async def save_and_load_file_in_partition():
    key = "VALIDFILEWITHTS"
    fs = FileStorage(path=f"/tmp/{key}/", partition_dateformat="%Y/%m/%d")
    path = await fs.store(key, test_fs_with_ts)
    assert path == f"/tmp/{key}/2022/03/01/{key}.json"
    loaded = await fs.get(key, datatype=FsMockData, partition_key="2022/03/01")
    assert loaded == test_fs_with_ts
    assert type(loaded) is FsMockData


async def save_and_load_file_in_partition_without_ts():
    key = "VALIDFILEWITHTS"
    fs = FileStorage(path=f"/tmp/{key}/", partition_dateformat="%Y/%m/%d")
    path = await fs.store(key, test_fs_without_ts)
    partition_key = datetime.now(tz=timezone.utc).strftime("%Y/%m/%d")
    assert path == f"/tmp/{key}/{partition_key}/{key}.json"
    loaded = await fs.get(key, datatype=FsMockDataNoTs, partition_key="2022/03/01")
    assert loaded == test_fs_without_ts
    assert type(loaded) is FsMockDataNoTs


async def save_and_load_file_in_partition_default_ts():
    key = "VALIDFILE"
    fs = FileStorage(path=f"/tmp/{key}/", partition_dateformat="%Y/%m/%d")
    path = await fs.store(key, test_fs)
    partition_key = datetime.now(tz=timezone.utc).strftime("%Y/%m/%d")
    assert path == f"/tmp/{key}/{partition_key}/{key}.json"
    loaded = await fs.get(key, datatype=FsMockData, partition_key=partition_key)
    assert loaded == test_fs
    assert type(loaded) is FsMockData


async def load_missing_file():
    key = "FILENOTFOUND"
    fs = FileStorage(path=f"/tmp/{key}/")
    loaded = await fs.get(key, datatype=FsMockData)
    assert loaded is None


class MockFile:
    def __init__(self, payload_str: str):
        self.payload_str = payload_str

    def __enter__(self):
        return self

    def __aenter__(self):
        return self

    def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.await_nothing()

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    async def await_nothing(self):
        return self

    def __await__(self):
        return self.await_nothing().__await__()

    async def read(self):
        return self.payload_str

    async def write(self, payload_str):
        self.payload_str = payload_str

    async def flush(self):
        pass

    @staticmethod
    def open(path, mode="r", encoding: str = ""):
        assert path
        assert mode
        if "b" not in mode:
            assert encoding == "utf-8"
        if "FILENOTFOUND" in str(path):
            raise FileNotFoundError(str(path))
        elif "VALIDFILEWITHTS" in str(path):
            return MockFile(payload_str_with_ts)
        elif "VALIDFILE" in str(path):
            return MockFile(payload_str)
        else:
            raise NotImplementedError()


class MockPath:
    @staticmethod
    def exists(path):
        assert path
        return False


class MockOs:
    @staticmethod
    def rename(source, dest):
        pass

    @staticmethod
    def makedirs(path, mode=None, exist_ok=False):
        pass


def mock_glob(wc: str) -> List[str]:
    if wc == "/path/*.json":
        return ["/path/1.json", "/path/2.json", "/path/3.json"]
    elif wc == "/path/1*.json":
        return ["/path/1.json"]
    elif wc == "/path/*":
        return ["/path/1.json", "/path/2.json", "/path/3.json"]
    elif wc == "/path/1*":
        return ["/path/1.json"]
    raise ValueError(f"glob received unexpected wildcard: {wc}")


def mock_glob_partitions(wc: str) -> List[str]:
    if wc == "/path/*.json":
        return []
    elif wc == "/path/2022/03/01/*.json":
        return [
            "/path/2022/03/01/1.json",
            "/path/2022/03/01/2.json",
            "/path/2022/03/01/3.json",
        ]
    elif wc == "/path/**/**/**/1*.json":
        return ["/path/2022/03/01/1.json"]
    raise ValueError(f"glob received unexpected wildcard: {wc}")


@pytest.mark.asyncio
async def test_save_load_file(monkeypatch):
    monkeypatch.setattr(aiofiles, "open", MockFile.open)
    monkeypatch.setattr(os, "makedirs", MockOs.makedirs)
    monkeypatch.setattr(os.path, "exists", MockPath.exists)
    monkeypatch.setattr(os, "rename", MockOs.rename)
    await save_and_load_file()


@pytest.mark.asyncio
async def test_delete_file(monkeypatch):
    await delete_files()


@pytest.mark.asyncio
async def test_delete_file_in_partition(monkeypatch):
    await delete_files_in_partition()


@pytest.mark.asyncio
async def test_save_load_file_in_partition(monkeypatch):
    monkeypatch.setattr(aiofiles, "open", MockFile.open)
    monkeypatch.setattr(os, "makedirs", MockOs.makedirs)
    monkeypatch.setattr(os.path, "exists", MockPath.exists)
    monkeypatch.setattr(os, "rename", MockOs.rename)
    await save_and_load_file_in_partition()
    await save_and_load_file_in_partition_without_ts()


@pytest.mark.asyncio
async def test_save_load_file_in_partition_default_ts(monkeypatch):
    monkeypatch.setattr(aiofiles, "open", MockFile.open)
    monkeypatch.setattr(os, "makedirs", MockOs.makedirs)
    monkeypatch.setattr(os.path, "exists", MockPath.exists)
    monkeypatch.setattr(os, "rename", MockOs.rename)
    await save_and_load_file_in_partition_default_ts()


@pytest.mark.asyncio
async def test_load_missing_file(monkeypatch):
    monkeypatch.setattr(aiofiles, "open", MockFile.open)
    await load_missing_file()


@pytest.mark.asyncio
async def test_list_objects(monkeypatch):
    monkeypatch.setattr(os, "makedirs", MockOs.makedirs)
    monkeypatch.setattr(fs_module, "glob", mock_glob)
    fs = FileStorage(path="/path")

    objects = await fs.list_objects()
    assert objects == [
        ItemLocator("1", None),
        ItemLocator("2", None),
        ItemLocator("3", None),
    ]

    objects = await fs.list_objects(wildcard="1*")
    assert objects == [
        ItemLocator("1", None),
    ]

    files = await fs.list_files()
    assert files == [
        ItemLocator("1.json", None),
        ItemLocator("2.json", None),
        ItemLocator("3.json", None),
    ]

    files = await fs.list_files(wildcard="1*")
    assert files == [
        ItemLocator("1.json", None),
    ]


@pytest.mark.asyncio
async def test_list_objects_within_partitions(monkeypatch):
    monkeypatch.setattr(os, "makedirs", MockOs.makedirs)
    monkeypatch.setattr(fs_module, "glob", mock_glob_partitions)
    fs = FileStorage(path="/path", partition_dateformat="%Y/%m/%d/")

    files = await fs.list_objects()
    assert files == []

    files = await fs.list_objects(wildcard="2022/03/01/*")
    assert files == [
        ItemLocator("1", "2022/03/01"),
        ItemLocator("2", "2022/03/01"),
        ItemLocator("3", "2022/03/01"),
    ]

    files = await fs.list_objects(wildcard="**/**/**/1*")
    assert files == [ItemLocator("1", "2022/03/01")]


@pytest.mark.asyncio
async def test_get_store_file():
    key = "BINARYFILE"
    fs = FileStorage(path=f"/tmp/{key}/")
    file_obj = io.BytesIO(binary_file)
    location = await fs.store_file(key, file_obj)
    assert location == str(fs.path / key)
    loaded = await fs.get_file(key)
    assert loaded == binary_file


@pytest.mark.asyncio
async def test_get_store_file_in_partition():
    key = "BINARYFILE"
    fs = FileStorage(path=f"/tmp/{key}/", partition_dateformat="%Y/%m/%d")
    partition_key = datetime.now(tz=timezone.utc).strftime("%Y/%m/%d")
    file_obj = io.BytesIO(binary_file)
    location = await fs.store_file(key, file_obj)
    assert location == str(fs.path / f"{partition_key}/{key}")
    loaded = await fs.get_file(key, partition_key=partition_key)
    assert loaded == binary_file

    assert fs.partition_key(location) == partition_key


@pytest.mark.asyncio
async def test_with_settings():
    setting = FileStorageSettings(path="/tmp/", partition_dateformat="%Y/%m/%d")
    fs = FileStorage.with_settings(settings=setting)
    assert fs.path == Path("/tmp/")
    assert fs.partition_dateformat == "%Y/%m/%d"
