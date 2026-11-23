"""回收任务管理：后台线程执行、可中断、状态查询。

进程内状态不落盘——镜像文件本身就是持久状态：已打的洞保存在文件中，
进程重启后用 SEEK_DATA 扫描会自动跳过这些空洞，可随时重新发起回收。
"""
from __future__ import annotations

import threading
from typing import Optional

from .constants import STATE_DATA, STATE_HOLE
from .disk import DiskImage


class ReclaimManager:
    def __init__(self, disk: DiskImage):
        self.disk = disk
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()  # 仅保护任务元数据，不阻塞块读写
        self._reset_locked()

    def _reset_locked(self) -> None:
        self._running = False
        self._cancel_requested = False
        self._finished = False
        self._scanned = 0
        self._punched = 0
        self._skipped_data = 0
        self._last_index = -1
        self._error: Optional[str] = None
        self._actual_before = self.disk.actual_bytes()
        self._actual_after: Optional[int] = None

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            if self._running:
                return self._state_locked()
            self._reset_locked()
            self._running = True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="reclaim-worker", daemon=True
            )
            self._thread.start()
            return self._state_locked()

    def cancel(self) -> dict:
        """请求优雅中断：当前块处理完即停，已打的洞全部保留。"""
        with self._lock:
            if self._running:
                self._cancel_requested = True
                self._stop.set()
            return self._state_locked()

    def _run(self) -> None:
        try:
            for event in self.disk.reclaim(should_stop=self._stop.is_set):
                state = event["state"]
                with self._lock:
                    self._last_index = event["index"]
                    if state == "punched":
                        self._punched += 1
                        self._scanned += 1
                    elif state in (STATE_DATA, STATE_HOLE):
                        self._scanned += 1
                        if state == STATE_DATA:
                            self._skipped_data += 1
                    # "stopped" 事件不增加计数
        except Exception as exc:  # noqa: BLE001 - 任何异常都回传网页
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._running = False
                self._finished = True
                self._actual_after = self.disk.actual_bytes()

    # -- 状态 ---------------------------------------------------------------
    def _state_locked(self) -> dict:
        """调用方必须已持有 self._lock。"""
        actual = self.disk.actual_bytes()
        reclaimed = self._actual_before - actual
        return {
            "running": self._running,
            "cancel_requested": self._cancel_requested,
            "finished": self._finished,
            "error": self._error,
            "scanned_blocks": self._scanned,
            "punched_blocks": self._punched,
            "skipped_data_blocks": self._skipped_data,
            "last_index": self._last_index,
            "actual_bytes_before": self._actual_before,
            "actual_bytes_now": actual,
            "reclaimed_bytes": max(reclaimed, 0),
            "total_blocks": self.disk.block_count,
        }

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()
