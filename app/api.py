"""与 Web 框架无关的 API 逻辑，Flask 与 stdlib 回退入口共用。"""
from __future__ import annotations

import binascii

from .constants import BLOCK_SIZE
from .disk import DiskImage
from .reclaim import ReclaimManager


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ApiCore:
    def __init__(self, disk: DiskImage):
        self.disk = disk
        self.manager = ReclaimManager(disk)

    # -- GET /api/status ----------------------------------------------------
    def status(self) -> dict:
        snap = self.disk.snapshot()
        snap["reclaim"] = self.manager.state()
        return snap

    # -- GET /api/blocks/<i> ------------------------------------------------
    def get_block(self, index: int) -> dict:
        self._check_index(index)
        data = self.disk.read_block(index)
        return {
            "index": index,
            "state": self.disk.block_state(index),
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
        # 正常读写不等待回收扫描：块锁只在单个 4KiB 操作期间持有
        self.disk.write_block(index, data)
        return {"index": index, "written_bytes": BLOCK_SIZE, "state": self.disk.block_state(index)}

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
        self.disk.write_block(index, data)
        return {"index": index, "state": self.disk.block_state(index)}

    def _check_index(self, index: int) -> None:
        if not isinstance(index, int) or not (0 <= index < self.disk.block_count):
            raise ApiError(404, f"块号越界，合法范围 0..{self.disk.block_count - 1}")


def _ascii_preview(data: bytes, limit: int = 64) -> str:
    chunk = data[:limit]
    return "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
