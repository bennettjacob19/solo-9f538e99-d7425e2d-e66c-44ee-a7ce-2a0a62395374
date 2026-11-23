"""写回缓存 + WAL 刷盘：掉电演练的核心层。

语义
----
* **写**：4 KiB 块写入只进内存缓存，读块立即看到最新值；
  从未纳入刷盘的写入随进程退出丢弃（这正是掉电演练要观察的现象）。
* **刷盘（flush）**：一轮开始时对缓存做快照作为“本批”，本轮只提交这些内容；
  刷盘进行中到达的同块新写入立即可读，但留在缓存里等下一轮。
* **崩溃原子性（WAL）**：本批先整体写入日志文件（magic + 块记录 + SHA-256
  校验和，即恢复记录）并 fsync，再逐块写回 raw 镜像并 fsync，最后删除日志。
  任何时刻掉电，重启后：
    - 日志缺失或校验失败（未提交）→ 镜像从未被本轮触碰 → 整批丢弃；
    - 日志完整（已提交）→ 重放日志把整批补齐 → 整批保留。
  刷盘调用返回成功时，整批已写入原 raw 镜像并 fsync。
* **读写不等刷盘**：刷盘的磁盘 I/O（写日志、pwrite、fsync）不持有缓存锁，
  块读写只经过内存缓存与既有块锁，即使刷盘在等宿主盘写入也能完成。
* 每轮只写本批涉及的块与必要的恢复记录，不触碰其它块。

演练钩子
--------
``flush_delay_ms``（环境变量 ``FLUSH_DELAY_MS``）在“日志已提交、尚未回写”
以及每块回写前各睡一次，把原本毫秒级的窗口拉长到秒级，方便测试人员
在刷盘中途 kill -9 验证整批原子性。默认 0，不影响正常性能。
"""
from __future__ import annotations

import hashlib
import os
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

from .constants import BLOCK_SIZE, STATE_DATA, STATE_ZERO
from .disk import DiskImage

# 日志格式：magic(8) | count(uint64) | [index(uint64) | data(4096)] * count | sha256(32)
# 校验和覆盖从 magic 到最后一个块记录的全部字节；任何截断/篡改都导致校验失败。
_JOURNAL_MAGIC = b"VDFLUSH1"
_HEADER = struct.Struct("<8sQ")
_INDEX = struct.Struct("<Q")
_DIGEST_SIZE = hashlib.sha256().digest_size


class FlushBusyError(Exception):
    """上一轮刷盘尚未完成。"""


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _fsync_dir(path: str) -> None:
    """fsync 目录，保证日志文件的创建/删除本身持久。"""
    fd = os.open(path or ".", os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def encode_journal(batch: List[Tuple[int, bytes]]) -> bytes:
    """把一批 (块号, 4KiB 数据) 编码为带校验和的日志内容。"""
    hasher = hashlib.sha256()
    out = bytearray()
    header = _HEADER.pack(_JOURNAL_MAGIC, len(batch))
    out += header
    hasher.update(header)
    for index, data in batch:
        record = _INDEX.pack(index) + data
        out += record
        hasher.update(record)
    out += hasher.digest()
    return bytes(out)


def decode_journal(raw: bytes) -> Optional[List[Tuple[int, bytes]]]:
    """解析并校验日志；任何不完整/损坏都返回 None（视为未提交，整批丢弃）。"""
    if len(raw) < _HEADER.size + _DIGEST_SIZE:
        return None
    magic, count = _HEADER.unpack(raw[: _HEADER.size])
    if magic != _JOURNAL_MAGIC:
        return None
    expected = _HEADER.size + count * (_INDEX.size + BLOCK_SIZE) + _DIGEST_SIZE
    if len(raw) != expected:
        return None
    body, digest = raw[:-_DIGEST_SIZE], raw[-_DIGEST_SIZE:]
    if hashlib.sha256(body).digest() != digest:
        return None
    batch: List[Tuple[int, bytes]] = []
    pos = _HEADER.size
    for _ in range(count):
        (index,) = _INDEX.unpack(raw[pos : pos + _INDEX.size])
        pos += _INDEX.size
        data = raw[pos : pos + BLOCK_SIZE]
        pos += BLOCK_SIZE
        batch.append((index, data))
    return batch


class WriteBackCache:
    """DiskImage 前的写回缓存；刷盘经 WAL 保证整批原子。"""

    def __init__(self, disk: DiskImage, journal_path: Optional[str] = None, flush_delay_ms: int = 0):
        self.disk = disk
        self.journal_path = journal_path or (disk.path + ".journal")
        self._flush_delay = max(0, int(flush_delay_ms)) / 1000.0
        self._lock = threading.Lock()          # 只保护缓存与轮次元数据，磁盘 I/O 不持有它
        self._cache: Dict[int, Tuple[int, bytes]] = {}  # index -> (seq, data)
        self._seq = 0
        self._flush_lock = threading.Lock()    # 同一时刻只允许一轮刷盘
        self._round_running = False
        self._last_started_at: Optional[float] = None
        self._last_finished_at: Optional[float] = None
        self._last_batch_blocks = 0
        self._last_duration_ms: Optional[int] = None
        self._last_error: Optional[str] = None
        self._rounds_completed = 0
        # 启动即恢复：上一进程若死在刷盘中途，在这里补齐或丢弃
        self._recovered_blocks = self._recover()

    # -- 启动恢复 -----------------------------------------------------------
    def _recover(self) -> int:
        """重放已提交日志（整批留下）或丢弃残缺日志（整批丢弃）。返回重放块数。"""
        try:
            with open(self.journal_path, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return 0
        except OSError:
            return 0
        batch = decode_journal(raw) if raw else None
        recovered = 0
        if batch is not None:
            # 重放是幂等的：整批重写一遍，无论上次回写停在哪一块
            for index, data in batch:
                if 0 <= index < self.disk.block_count:
                    self.disk.write_block(index, data)
                    recovered += 1
            os.fsync(self.disk._fd)
        # 无论重放还是丢弃，日志使命都已结束；删除并保证删除持久
        try:
            os.unlink(self.journal_path)
            _fsync_dir(os.path.dirname(os.path.abspath(self.journal_path)))
        except OSError:
            pass
        return recovered

    # -- 块读写（不等待刷盘） -----------------------------------------------
    def read_block(self, index: int) -> bytes:
        with self._lock:
            entry = self._cache.get(index)
        if entry is not None:
            return entry[1]  # bytes 不可变，可直接交出
        return self.disk.read_block(index)

    def write_block(self, index: int, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)) or len(data) != BLOCK_SIZE:
            raise ValueError(f"写入数据必须恰好为 {BLOCK_SIZE} 字节")
        with self._lock:
            self._seq += 1
            self._cache[index] = (self._seq, bytes(data))

    def block_state(self, index: int) -> str:
        """缓存视角的块状态：脏块按缓存内容判定，干净块看 raw 镜像。"""
        with self._lock:
            entry = self._cache.get(index)
        if entry is not None:
            return STATE_ZERO if entry[1] == b"\x00" * BLOCK_SIZE else STATE_DATA
        return self.disk.block_state(index)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._cache)

    # -- 刷盘 ---------------------------------------------------------------
    def flush(self) -> dict:
        """同步执行一轮刷盘；返回时整批已写入 raw 镜像并 fsync。

        一轮未结束时再次调用抛出 FlushBusyError。
        """
        if not self._flush_lock.acquire(blocking=False):
            raise FlushBusyError("上一轮刷盘尚未完成")
        try:
            return self._flush_round()
        finally:
            self._flush_lock.release()

    def _drill_sleep(self) -> None:
        if self._flush_delay:
            time.sleep(self._flush_delay)

    def _flush_round(self) -> dict:
        started_mono = time.monotonic()
        # 快照即“本批”：只提交开始时已在缓存的内容；之后的写入留到下一轮
        with self._lock:
            batch = [(idx, seq, data) for idx, (seq, data) in sorted(self._cache.items())]
            self._round_running = True
            self._last_started_at = time.time()
            self._last_error = None
            self._last_batch_blocks = len(batch)
        try:
            if batch:
                # 1) 恢复记录整体落盘（提交点）
                self._commit_journal([(idx, data) for idx, _, data in batch])
                self._drill_sleep()  # 演练窗口：已提交、尚未回写
                # 2) 逐块写回 raw 镜像（沿用块锁，与回收打洞互斥错开）
                for idx, _seq, data in batch:
                    self._drill_sleep()  # 演练窗口：回写中途
                    self.disk.write_block(idx, data)
                os.fsync(self.disk._fd)
                # 3) 整批已在镜像中持久，删除恢复记录
                os.unlink(self.journal_path)
                _fsync_dir(os.path.dirname(os.path.abspath(self.journal_path)))
            with self._lock:
                # 只移除仍是本轮快照版本的条目；刷盘期间被覆盖的同块新写入
                # 序号不同，保留在缓存里等下一轮
                for idx, seq, _data in batch:
                    current = self._cache.get(idx)
                    if current is not None and current[0] == seq:
                        del self._cache[idx]
                self._rounds_completed += 1
                self._round_running = False
                self._last_finished_at = time.time()
                self._last_duration_ms = int((time.monotonic() - started_mono) * 1000)
                duration_ms = self._last_duration_ms
            return {"committed_blocks": len(batch), "duration_ms": duration_ms}
        except Exception as exc:  # noqa: BLE001 - 记录后原样抛出，由 API 层转 500
            with self._lock:
                self._round_running = False
                self._last_error = f"{type(exc).__name__}: {exc}"
            raise

    def _commit_journal(self, blocks: List[Tuple[int, bytes]]) -> None:
        payload = encode_journal(blocks)
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(os.path.dirname(os.path.abspath(self.journal_path)))

    # -- 状态 ---------------------------------------------------------------
    def flush_state(self) -> dict:
        with self._lock:
            return {
                "running": self._round_running,
                "pending_blocks": len(self._cache),
                "dirty_blocks": sorted(self._cache),
                "last_batch_blocks": self._last_batch_blocks,
                "last_started_at": self._last_started_at,
                "last_finished_at": self._last_finished_at,
                "last_duration_ms": self._last_duration_ms,
                "last_error": self._last_error,
                "rounds_completed": self._rounds_completed,
                "recovered_blocks": self._recovered_blocks,
                "journal_path": self.journal_path,
                "flush_delay_ms": int(self._flush_delay * 1000),
            }
