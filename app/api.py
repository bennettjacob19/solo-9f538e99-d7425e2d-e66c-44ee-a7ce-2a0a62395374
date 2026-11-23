"""与 Web 框架无关的 API 逻辑，Flask 与 stdlib 回退入口共用。

块读写走写回缓存（WriteBackCache）：写入先进内存缓存、读立即生效，
刷盘才落 raw 镜像；零块回收仍直接作用于 raw 镜像，两者互不影响。
"""
from __future__ import annotations

import binascii

from .cache import FlushBusyError, WriteBackCache
from .constants import BLOCK_SIZE
from .disk import DiskImage
from .reclaim import ReclaimManager


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ApiCore:
    def __init__(self, disk: DiskImage, flush_delay_ms: int = 0):
        self.disk = disk
        self.cache = WriteBackCache(disk, flush_delay_ms=flush_delay_ms)
        self.manager = ReclaimManager(disk)

    # -- GET /api/status ----------------------------------------------------
    def status(self) -> dict:
        return {
            "stats": self.disk.stats(),
            # 缓存视角的块状态：脏块按缓存内容展示，与读到的值一致
            "blocks": [self.cache.block_state(i) for i in range(self.disk.block_count)],
            "reclaim": self.manager.state(),
            "flush": self.cache.flush_state(),
        }

    # -- GET /api/blocks/<i> ------------------------------------------------
    def get_block(self, index: int) -> dict:
        self._check_index(index)
        data = self.cache.read_block(index)
        return {
            "index": index,
            "state": self.cache.block_state(index),
            "hex": data.hex(),
            "ascii_preview": _ascii_preview(data),
        }

    # -- POST /api/blocks/<i>/read （虚拟机读，返回块值，不改变任何状态） ----
    def read_block(self, index: int) -> dict:
        return self.get_block(index)

    # -- POST /api/blocks/<i>/write ----------------------------------------
    def write_block(self, index: int, payload: dict) -> dict:
        self._check_index(index)
        if not isinstance(payload, dict) or "hex" not in payload:
            raise ApiError(400, "请求体需为 JSON 且包含 hex 字段")
        try:
            data = bytes.fromhex(payload["hex"])
        except (ValueError, TypeError, binascii.Error):
            raise ApiError(400, "hex 字段不是合法的十六进制字符串")
        if len(data) != BLOCK_SIZE:
            raise ApiError(
                400, f"一次写入必须恰好 {BLOCK_SIZE} 字节，收到 {len(data)} 字节"
            )
        # 写入只进缓存，立即返回；刷盘前不触碰 raw 镜像，也不等待进行中的刷盘
        self.cache.write_block(index, data)
        return {
            "index": index,
            "written_bytes": BLOCK_SIZE,
            "state": self.cache.block_state(index),
            "pending_blocks": self.cache.pending_count(),
        }

    # -- POST /api/flush ----------------------------------------------------
    def flush(self) -> dict:
        """同步刷盘：返回成功时整批已写入 raw 镜像；进行中则 409 忙。"""
        try:
            result = self.cache.flush()
        except FlushBusyError:
            raise ApiError(409, "上一轮刷盘尚未完成，请稍后再试")
        return {"ok": True, **result, "pending_blocks": self.cache.pending_count()}

    # -- POST /api/reclaim/start、/cancel -----------------------------------
    def reclaim_start(self) -> dict:
        return self.manager.start()

    def reclaim_cancel(self) -> dict:
        return self.manager.cancel()

    # -- 便捷操作：写全零 / 写非零图案，方便网页演示 -------------------------
    def write_pattern(self, index: int, kind: str) -> dict:
        self._check_index(index)
        if kind == "zero":
            data = b"\x00" * BLOCK_SIZE
        elif kind == "data":
            marker = f"web-write-block-{index:04d}\n".encode()
            data = (marker + b"\x5a" * BLOCK_SIZE)[:BLOCK_SIZE]
        else:
            raise ApiError(400, "未知 pattern，仅支持 zero / data")
        self.cache.write_block(index, data)
        return {
            "index": index,
            "state": self.cache.block_state(index),
            "pending_blocks": self.cache.pending_count(),
        }

    def _check_index(self, index: int) -> None:
        if not isinstance(index, int) or not (0 <= index < self.disk.block_count):
            raise ApiError(404, f"块号越界，合法范围 0..{self.disk.block_count - 1}")


def _ascii_preview(data: bytes, limit: int = 64) -> str:
    chunk = data[:limit]
    return "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
