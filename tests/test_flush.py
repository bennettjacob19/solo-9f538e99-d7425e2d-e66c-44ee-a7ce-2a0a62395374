"""刷盘（写回缓存 + WAL）端到端自测：纯标准库 unittest，不依赖 Flask。

覆盖：
1. 写入先进缓存：读立即生效、raw 镜像未变、待刷盘计数
2. 刷盘落盘：整批写入 raw 镜像、恢复记录清理、计数归零；空缓存为空操作
3. 同块覆盖：刷盘进行中的新写入立即可读、留到下一轮（快照语义）
4. 忙：一轮未结束再次刷盘 → FlushBusyError / API 409
5. 刷盘等待宿主盘期间，块读写照常完成
6. 未刷盘写入随“进程退出”丢弃
7. 崩溃恢复：已提交日志整批重放（含部分回写现场）、残缺日志整批丢弃
8. 回收与缓存共存：回收作用于 raw 镜像，缓存留在内存
9. 真实 kill -9 子进程演练：提交后杀 → 整批保留；回写中途杀 → 重放补齐；
   刷盘完成后杀 → 数据保留；未刷盘杀 → 整批丢弃
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from app.api import ApiCore, ApiError
from app.cache import FlushBusyError, WriteBackCache, decode_journal, encode_journal
from app.constants import BLOCK_SIZE, STATE_DATA, STATE_HOLE, STATE_ZERO
from app.disk import create_image
from app.server_stdlib import create_wsgi_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 掉电演练子进程：写入一批块后按模式行动，通过 stdout 报告进度，
# 父进程在指定进度点 kill -9，再用同一数据目录“重启”验证整批原子性。
CHILD_SCRIPT = r"""
import sys
import time

from app.cache import WriteBackCache
from app.constants import BLOCK_SIZE
from app.disk import create_image

path, mode, blocks, delay_ms = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
disk = create_image(path, 32)
cache = WriteBackCache(disk, flush_delay_ms=delay_ms)
for i in range(blocks):
    data = (f"child-{i:02d}|".encode() + bytes([65 + i]) * BLOCK_SIZE)[:BLOCK_SIZE]
    cache.write_block(i, data)
print("READY", flush=True)
if mode == "flush":
    orig_commit = cache._commit_journal

    def commit(batch):
        orig_commit(batch)
        print("JOURNALED", flush=True)

    cache._commit_journal = commit
    orig_write = disk.write_block

    def write_block(index, data):
        print(f"APPLY {index}", flush=True)
        return orig_write(index, data)

    disk.write_block = write_block
    cache.flush()
    print("FLUSHED", flush=True)
else:
    time.sleep(30)
"""


def start_child(path, mode, blocks=6, delay_ms=500):
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.Popen(
        [sys.executable, "-c", CHILD_SCRIPT, path, mode, str(blocks), str(delay_ms)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def read_until(proc, wanted):
    """逐行读子进程 stdout，直到出现以 wanted 开头的行；EOF 则报错。"""
    lines = []
    while True:
        line = proc.stdout.readline()
        if line == "":
            stderr = proc.stderr.read()
            raise AssertionError(f"子进程未输出 {wanted!r}，已收到 {lines}，stderr: {stderr}")
        lines.append(line.strip())
        if line.strip().startswith(wanted):
            return lines


def stop_child(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass


def wsgi_call(app, method, path, body=None):
    req = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)) if body else "0",
        "wsgi.input": io.BytesIO(body or b""),
    }
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    raw = b"".join(app(req, start_response))
    return int(captured["status"].split(" ")[0]), json.loads(raw.decode())


class TempImageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flush-test-")
        self.path = os.path.join(self.tmp, "disk.raw")
        self.journal_path = self.path + ".journal"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open_disk(self, blocks=32):
        return create_image(self.path, blocks)


class TestJournalCodec(unittest.TestCase):
    def test_roundtrip(self):
        batch = [(0, b"\x00" * BLOCK_SIZE), (7, b"\xff" * BLOCK_SIZE)]
        self.assertEqual(decode_journal(encode_journal(batch)), batch)

    def test_reject_garbage(self):
        self.assertIsNone(decode_journal(b""))
        self.assertIsNone(decode_journal(b"VDFLUSH1"))
        raw = encode_journal([(1, b"\x01" * BLOCK_SIZE)])
        self.assertIsNone(decode_journal(raw[:-1]))  # 截断
        bad = bytearray(raw)
        bad[20] ^= 0xFF
        self.assertIsNone(decode_journal(bytes(bad)))  # 内容损坏，校验和不匹配


class TestWriteBackCache(TempImageTestCase):
    def test_write_goes_to_cache_read_sees_latest(self):
        disk = self.open_disk(32)
        cache = WriteBackCache(disk)
        try:
            payload = (b"CACHED" + b"\x42" * BLOCK_SIZE)[:BLOCK_SIZE]
            cache.write_block(0, payload)
            self.assertEqual(cache.read_block(0), payload)  # 读立即看到最新值
            self.assertEqual(disk.read_block(0), b"\x00" * BLOCK_SIZE)  # raw 镜像未变
            self.assertEqual(cache.pending_count(), 1)
            self.assertEqual(cache.block_state(0), STATE_DATA)  # 缓存视角
            self.assertEqual(disk.block_state(0), STATE_HOLE)  # 镜像仍是空洞
        finally:
            disk.close()

    def test_flush_commits_batch_and_cleans_journal(self):
        disk = self.open_disk(32)
        cache = WriteBackCache(disk)
        try:
            p0 = (b"BLOCK-0" + b"\x10" * BLOCK_SIZE)[:BLOCK_SIZE]
            p5 = (b"BLOCK-5" + b"\x15" * BLOCK_SIZE)[:BLOCK_SIZE]
            cache.write_block(0, p0)
            cache.write_block(5, p5)
            result = cache.flush()
            self.assertEqual(result["committed_blocks"], 2)
            self.assertEqual(cache.pending_count(), 0)
            self.assertEqual(disk.read_block(0), p0)  # 整批已写入 raw 镜像
            self.assertEqual(disk.read_block(5), p5)
            self.assertFalse(os.path.exists(self.journal_path))  # 恢复记录已清理

            empty = cache.flush()  # 空缓存刷盘是空操作，不产生日志
            self.assertEqual(empty["committed_blocks"], 0)
            self.assertFalse(os.path.exists(self.journal_path))
        finally:
            disk.close()

    def test_same_block_overwrite_during_flush_stays_for_next_round(self):
        """同块覆盖：刷盘进行中的新写入立即可读，但留到下一轮刷盘。"""
        disk = self.open_disk(32)
        cache = WriteBackCache(disk, flush_delay_ms=300)
        v1 = (b"VERSION-1" + b"\x01" * BLOCK_SIZE)[:BLOCK_SIZE]
        v2 = (b"VERSION-2" + b"\x02" * BLOCK_SIZE)[:BLOCK_SIZE]
        try:
            cache.write_block(3, v1)
            done = {}

            def run():
                done["r1"] = cache.flush()

            t = threading.Thread(target=run)
            t.start()
            # delay=300ms：日志提交后、回写开始前各有 300ms 窗口，回写在 ~600ms
            time.sleep(0.15)
            cache.write_block(3, v2)  # 同块新写入
            self.assertEqual(cache.read_block(3), v2)  # 立即可读
            self.assertEqual(disk.read_block(3), b"\x00" * BLOCK_SIZE)  # 镜像尚未动
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
            self.assertEqual(done["r1"]["committed_blocks"], 1)
            self.assertEqual(disk.read_block(3), v1)  # 本轮只提交开始时的 v1
            self.assertEqual(cache.read_block(3), v2)  # v2 仍在缓存
            self.assertEqual(cache.pending_count(), 1)

            r2 = cache.flush()  # 下一轮才提交 v2
            self.assertEqual(r2["committed_blocks"], 1)
            self.assertEqual(disk.read_block(3), v2)
            self.assertEqual(cache.pending_count(), 0)
        finally:
            disk.close()

    def test_busy_while_flush_running(self):
        disk = self.open_disk(32)
        cache = WriteBackCache(disk, flush_delay_ms=300)
        payload = b"\x01" * BLOCK_SIZE
        try:
            cache.write_block(1, payload)
            cache.write_block(2, payload)
            t = threading.Thread(target=cache.flush)
            t.start()
            time.sleep(0.1)  # 第一轮进行中（共约 0.9s）
            with self.assertRaises(FlushBusyError):
                cache.flush()
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
            self.assertEqual(cache.pending_count(), 0)  # 第一轮正常完成
        finally:
            disk.close()

    def test_block_io_completes_while_flush_waits_on_disk(self):
        """刷盘在等宿主盘写入（演练睡眠）时，块读写照常完成。"""
        disk = self.open_disk(32)
        cache = WriteBackCache(disk, flush_delay_ms=400)
        p1 = (b"FLUSHING" + b"\x06" * BLOCK_SIZE)[:BLOCK_SIZE]
        other = (b"OTHER" + b"\x07" * BLOCK_SIZE)[:BLOCK_SIZE]
        try:
            cache.write_block(1, p1)
            t = threading.Thread(target=cache.flush)
            t.start()
            time.sleep(0.1)  # 刷盘处于“等宿主盘”的演练窗口（共约 0.8s）
            start = time.monotonic()
            cache.write_block(2, other)  # 写不阻塞
            self.assertEqual(cache.read_block(2), other)  # 读不阻塞
            self.assertEqual(cache.read_block(1), p1)  # 脏块读缓存
            self.assertLess(time.monotonic() - start, 0.3)
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
            self.assertEqual(disk.read_block(1), p1)  # 第一轮只含块 1
            self.assertEqual(cache.pending_count(), 1)  # 块 2 留到下一轮
        finally:
            disk.close()

    def test_unflushed_writes_lost_on_restart(self):
        disk = self.open_disk(32)
        cache = WriteBackCache(disk)
        payload = (b"VOLATILE" + b"\x09" * BLOCK_SIZE)[:BLOCK_SIZE]
        cache.write_block(0, payload)
        self.assertEqual(cache.read_block(0), payload)
        disk.close()  # 模拟进程退出：缓存随之消失

        disk2 = create_image(self.path, 32)
        cache2 = WriteBackCache(disk2)
        try:
            self.assertEqual(cache2.read_block(0), b"\x00" * BLOCK_SIZE)
            self.assertEqual(cache2.pending_count(), 0)
            self.assertEqual(cache2.flush_state()["recovered_blocks"], 0)
        finally:
            disk2.close()

    def test_recovery_replays_committed_journal(self):
        """已提交日志 + 只回写了一半的现场 → 重启重放，整批补齐。"""
        disk = self.open_disk(32)
        b1 = (b"JOURNAL-1" + b"\x11" * BLOCK_SIZE)[:BLOCK_SIZE]
        b2 = (b"JOURNAL-2" + b"\x22" * BLOCK_SIZE)[:BLOCK_SIZE]
        with open(self.journal_path, "wb") as fh:
            fh.write(encode_journal([(1, b1), (2, b2)]))
        disk.write_block(1, b1)  # 块 1 已回写，块 2 未回写（部分回写现场）
        disk.close()

        disk2 = create_image(self.path, 32)
        cache2 = WriteBackCache(disk2)
        try:
            self.assertEqual(cache2.flush_state()["recovered_blocks"], 2)
            self.assertEqual(disk2.read_block(1), b1)
            self.assertEqual(disk2.read_block(2), b2)  # 重放补齐
            self.assertEqual(cache2.pending_count(), 0)
            self.assertFalse(os.path.exists(self.journal_path))
        finally:
            disk2.close()

    def test_recovery_discards_incomplete_journal(self):
        """残缺日志（截断/篡改/垃圾）→ 整批丢弃，镜像保持原样。"""
        disk = self.open_disk(32)
        b1 = (b"JOURNAL-1" + b"\x11" * BLOCK_SIZE)[:BLOCK_SIZE]
        b2 = (b"JOURNAL-2" + b"\x22" * BLOCK_SIZE)[:BLOCK_SIZE]
        full = encode_journal([(1, b1), (2, b2)])
        corrupted = bytearray(full)
        corrupted[-1] ^= 0xFF  # 校验和不匹配
        try:
            for bad in (full[: len(full) // 2], bytes(corrupted), b"garbage"):
                with open(self.journal_path, "wb") as fh:
                    fh.write(bad)
                cache = WriteBackCache(disk)
                self.assertEqual(cache.flush_state()["recovered_blocks"], 0)
                self.assertEqual(disk.read_block(1), b"\x00" * BLOCK_SIZE)  # 整批丢弃
                self.assertEqual(disk.read_block(2), b"\x00" * BLOCK_SIZE)
                self.assertFalse(os.path.exists(self.journal_path))  # 残缺日志被清理
        finally:
            disk.close()

    def test_reclaim_works_on_image_cache_stays_in_memory(self):
        """回收作用于 raw 镜像、与刷盘错开；触发回收时缓存留在内存。"""
        disk = self.open_disk(32)
        cache = WriteBackCache(disk)
        zeros = b"\x00" * BLOCK_SIZE
        nonzero = (b"DIRTY" + b"\x5c" * BLOCK_SIZE)[:BLOCK_SIZE]
        try:
            cache.write_block(0, zeros)  # 空洞块 → 缓存零
            cache.write_block(8, nonzero)  # 预置零块 → 缓存非零
            list(disk.reclaim())  # 回收只看得见 raw 镜像：块 8 在镜像上仍是零块，被打洞
            self.assertEqual(disk.block_state(8), STATE_HOLE)
            self.assertEqual(cache.pending_count(), 2)  # 缓存留在内存
            self.assertEqual(cache.read_block(0), zeros)
            self.assertEqual(cache.read_block(8), nonzero)  # 缓存数据不丢

            cache.flush()  # 刷盘后镜像才反映缓存内容
            self.assertEqual(disk.block_state(0), STATE_ZERO)
            self.assertEqual(disk.block_state(8), STATE_DATA)

            list(disk.reclaim())  # 再回收：落盘的零块被打洞，读值不变
            self.assertEqual(disk.block_state(0), STATE_HOLE)
            self.assertEqual(cache.read_block(0), zeros)
            self.assertEqual(disk.read_block(8), nonzero)
        finally:
            disk.close()


class TestFlushApi(TempImageTestCase):
    def setUp(self):
        super().setUp()
        self.disk = self.open_disk(16)
        self.core = ApiCore(self.disk)

    def tearDown(self):
        self.disk.close()
        super().tearDown()

    def test_flush_endpoint_and_status(self):
        payload = (b"API-FLUSH" + b"\x55" * BLOCK_SIZE)[:BLOCK_SIZE]
        resp = self.core.write_block(0, {"hex": payload.hex()})
        self.assertEqual(resp["pending_blocks"], 1)
        st = self.core.status()["flush"]
        self.assertEqual(st["pending_blocks"], 1)
        self.assertEqual(st["dirty_blocks"], [0])

        result = self.core.flush()
        self.assertTrue(result["ok"])
        self.assertEqual(result["committed_blocks"], 1)
        self.assertEqual(result["pending_blocks"], 0)
        self.assertEqual(self.disk.read_block(0), payload)  # 已写入 raw 镜像
        st = self.core.status()["flush"]
        self.assertEqual(st["pending_blocks"], 0)
        self.assertEqual(st["rounds_completed"], 1)
        self.assertEqual(st["last_batch_blocks"], 1)

    def test_flush_busy_returns_409(self):
        core = ApiCore(self.disk, flush_delay_ms=400)
        core.write_block(0, {"hex": (b"\x01" * BLOCK_SIZE).hex()})
        t = threading.Thread(target=core.flush)
        t.start()
        time.sleep(0.1)  # 第一轮进行中
        try:
            with self.assertRaises(ApiError) as ctx:
                core.flush()
            self.assertEqual(ctx.exception.status, 409)
            self.assertIn("尚未完成", ctx.exception.message)
        finally:
            t.join(timeout=5)
        self.assertFalse(t.is_alive())

    def test_wsgi_flush_route(self):
        app = create_wsgi_app(self.core)
        code, body = wsgi_call(
            app, "POST", "/api/blocks/0/write",
            json.dumps({"hex": (b"\x03" * BLOCK_SIZE).hex()}).encode(),
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["pending_blocks"], 1)

        code, body = wsgi_call(app, "POST", "/api/flush")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["committed_blocks"], 1)

        code, body = wsgi_call(app, "GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["flush"]["pending_blocks"], 0)
        self.assertEqual(body["flush"]["rounds_completed"], 1)

    def test_wsgi_flush_busy_409(self):
        core = ApiCore(self.disk, flush_delay_ms=400)
        app = create_wsgi_app(core)
        core.write_block(0, {"hex": (b"\x02" * BLOCK_SIZE).hex()})
        first = {}

        def run_first():
            first["result"] = wsgi_call(app, "POST", "/api/flush")

        t = threading.Thread(target=run_first)
        t.start()
        time.sleep(0.1)
        try:
            code, body = wsgi_call(app, "POST", "/api/flush")
            self.assertEqual(code, 409)
            self.assertIn("尚未完成", body["error"])
        finally:
            t.join(timeout=5)
        self.assertEqual(first["result"][0], 200)
        self.assertEqual(first["result"][1]["committed_blocks"], 1)


class TestKillRestart(TempImageTestCase):
    """真实 kill -9 子进程演练：同一数据目录“重启”后验证整批原子性。"""

    blocks = 6

    def _expected(self, i):
        return (f"child-{i:02d}|".encode() + bytes([65 + i]) * BLOCK_SIZE)[:BLOCK_SIZE]

    def _reopen(self):
        disk = create_image(self.path, 32)
        return disk, WriteBackCache(disk)

    def test_kill_after_commit_batch_survives(self):
        """恢复记录已提交、回写未开始时掉电 → 重启重放，整批保留。"""
        proc = start_child(self.path, "flush", self.blocks, delay_ms=500)
        try:
            read_until(proc, "JOURNALED")
        finally:
            stop_child(proc)  # 掉电：子进程死在回写前的演练窗口
        self.assertTrue(os.path.exists(self.journal_path))  # 恢复记录已提交

        disk, cache = self._reopen()
        try:
            self.assertEqual(cache.flush_state()["recovered_blocks"], self.blocks)
            for i in range(self.blocks):
                self.assertEqual(disk.read_block(i), self._expected(i))  # 整批保留
            self.assertEqual(cache.pending_count(), 0)
            self.assertFalse(os.path.exists(self.journal_path))  # 恢复记录已清理
        finally:
            disk.close()

    def test_kill_mid_apply_replay_completes_batch(self):
        """回写中途掉电（部分块已写）→ 重启重放，整批补齐。"""
        proc = start_child(self.path, "flush", self.blocks, delay_ms=500)
        try:
            read_until(proc, "APPLY 2")  # 块 0、1 已回写，2..5 未回写
        finally:
            stop_child(proc)
        self.assertTrue(os.path.exists(self.journal_path))

        disk, cache = self._reopen()
        try:
            self.assertEqual(cache.flush_state()["recovered_blocks"], self.blocks)
            for i in range(self.blocks):
                self.assertEqual(disk.read_block(i), self._expected(i))  # 整批补齐
            self.assertFalse(os.path.exists(self.journal_path))
        finally:
            disk.close()

    def test_kill_after_successful_flush_keeps_data(self):
        """刷盘返回成功后掉电 → 数据已在 raw 镜像，重启保留。"""
        proc = start_child(self.path, "flush", self.blocks, delay_ms=50)
        try:
            read_until(proc, "FLUSHED")
        finally:
            stop_child(proc)
        self.assertFalse(os.path.exists(self.journal_path))  # 成功后恢复记录已删除

        disk, cache = self._reopen()
        try:
            self.assertEqual(cache.flush_state()["recovered_blocks"], 0)  # 无需恢复
            for i in range(self.blocks):
                self.assertEqual(disk.read_block(i), self._expected(i))
        finally:
            disk.close()

    def test_kill_without_flush_drops_writes(self):
        """从未纳入刷盘的写入 → 随进程退出丢弃。"""
        proc = start_child(self.path, "hold", self.blocks, delay_ms=0)
        try:
            read_until(proc, "READY")
        finally:
            stop_child(proc)
        self.assertFalse(os.path.exists(self.journal_path))  # 从未提交

        disk, cache = self._reopen()
        try:
            self.assertEqual(cache.flush_state()["recovered_blocks"], 0)
            for i in range(self.blocks):
                self.assertEqual(disk.read_block(i), b"\x00" * BLOCK_SIZE)  # 仍是预置空洞
        finally:
            disk.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
