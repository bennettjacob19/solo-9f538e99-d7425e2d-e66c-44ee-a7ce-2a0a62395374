"""端到端自测：纯标准库 unittest，不依赖 Flask。

覆盖：
1. 预置镜像布局（空洞 / 写过的零块 / 非零块）与实占统计
2. 完整回收：只打零块、容量不变、读值不变、实占下降
3. 并发写入竞争：判零之后写入非零数据，回收不得抹掉新数据
4. 中断重跑：中断后原盘与已打的洞保留，重启可重新发起并继续回收
5. API 层输入校验与路由行为（stdlib WSGI 入口）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import unittest

from app.api import ApiCore, ApiError
from app.constants import BLOCK_SIZE, STATE_DATA, STATE_HOLE, STATE_ZERO
from app.disk import DiskImage, create_image
from app.reclaim import ReclaimManager
from app.server_stdlib import create_wsgi_app


class TempImageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reclaim-test-")
        self.path = os.path.join(self.tmp, "disk.raw")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open_disk(self, blocks=32):
        return create_image(self.path, blocks)


class TestPresetImage(TempImageTestCase):
    def test_layout_and_actual_bytes(self):
        disk = self.open_disk(32)  # quarter=8
        try:
            self.assertEqual(disk.capacity, 32 * BLOCK_SIZE)
            self.assertEqual(disk.block_state(0), STATE_HOLE)      # 0..7 空洞
            self.assertEqual(disk.block_state(8), STATE_ZERO)      # 8..15 写过的零块
            self.assertEqual(disk.block_state(16), STATE_DATA)     # 16..23 非零
            self.assertEqual(disk.block_state(24), STATE_HOLE)     # 24..31 空洞

            # 已分配的是 8 个零块 + 8 个非零块 = 16 * 4KiB
            self.assertEqual(disk.actual_bytes(), 16 * BLOCK_SIZE)
            self.assertEqual(os.path.getsize(self.path), disk.capacity)
        finally:
            disk.close()

    def test_reopen_is_idempotent(self):
        d1 = self.open_disk(16)
        d1.close()
        before = os.path.getsize(self.path)
        d2 = self.open_disk(16)
        try:
            self.assertEqual(os.path.getsize(self.path), before)
            self.assertEqual(d2.block_state(4), STATE_ZERO)
        finally:
            d2.close()


class TestFullReclaim(TempImageTestCase):
    def test_reclaim_punches_only_zero_blocks(self):
        disk = self.open_disk(32)
        try:
            zero_indices = [i for i in range(32) if disk.block_state(i) == STATE_ZERO]
            data_indices = [i for i in range(32) if disk.block_state(i) == STATE_DATA]
            self.assertEqual(len(zero_indices), 8)
            before = disk.actual_bytes()
            zero_values = {i: disk.read_block(i) for i in zero_indices}
            data_values = {i: disk.read_block(i) for i in data_indices}

            events = list(disk.reclaim())
            punched = sorted(e["index"] for e in events if e["state"] == "punched")
            self.assertEqual(punched, sorted(zero_indices))

            # 容量不变、实占下降：8 个零块全部释放
            self.assertEqual(os.path.getsize(self.path), disk.capacity)
            self.assertEqual(disk.actual_bytes(), before - 8 * BLOCK_SIZE)

            # 打洞后块读值不变（零块读回仍全零，非零块原样）
            for i in zero_indices:
                self.assertEqual(disk.read_block(i), zero_values[i])
                self.assertEqual(disk.read_block(i), b"\x00" * BLOCK_SIZE)
                self.assertEqual(disk.block_state(i), STATE_HOLE)
            for i in data_indices:
                self.assertEqual(disk.read_block(i), data_values[i])
                self.assertEqual(disk.block_state(i), STATE_DATA)

            # 再跑一轮：没有可回收的块，实占不再变化
            second = list(disk.reclaim())
            self.assertTrue(all(e["state"] != "punched" for e in second))
            self.assertEqual(disk.actual_bytes(), before - 8 * BLOCK_SIZE)
        finally:
            disk.close()

    def test_writing_zero_allocates_host_block(self):
        disk = self.open_disk(16)
        try:
            hole_index = 0
            self.assertEqual(disk.block_state(hole_index), STATE_HOLE)
            disk.write_block(hole_index, b"\x00" * BLOCK_SIZE)
            self.assertEqual(disk.block_state(hole_index), STATE_ZERO)
            # 回收后该块重新变空洞，读回仍为零
            list(disk.reclaim())
            self.assertEqual(disk.block_state(hole_index), STATE_HOLE)
            self.assertEqual(disk.read_block(hole_index), b"\x00" * BLOCK_SIZE)
        finally:
            disk.close()


class TestConcurrentWrite(TempImageTestCase):
    def test_nonzero_write_after_zero_judgement_is_never_punched(self):
        """关键竞争：扫描已判定为零块后，该块在打洞前被写入非零数据。

        块锁保证“判零 + 打洞”与 write_block 互斥。本测试确定性复现：
        回收线程打完前一个零块、即将处理目标块时，写线程持有目标块锁
        完成非零写入；回收线程拿到锁后二次校验读到非零，必须跳过打洞，
        新数据原样保留。
        """
        disk = self.open_disk(32)
        try:
            target = 9  # 预置零块；块 8 是它前面的第一个零块
            self.assertEqual(disk.block_state(target), STATE_ZERO)
            new_payload = (b"RACED-NONZERO-DATA" + b"\x11" * BLOCK_SIZE)[:BLOCK_SIZE]

            first_block_done = threading.Event()
            events = []

            def worker():
                for event in disk.reclaim(
                    on_event=lambda ev: (
                        events.append(ev),
                        first_block_done.set()
                        if ev["index"] == 8 and ev["state"] == "punched"
                        else None,
                    )
                ):
                    pass

            t = threading.Thread(target=worker)
            target_lock = disk._block_locks[target]
            # 先持有目标块锁，再启动回收，回收处理到目标块时会阻塞
            with target_lock:
                t.start()
                self.assertTrue(first_block_done.wait(5))
                # 持锁写入（等价于 write_block 已获得块锁后的写动作）
                os.pwrite(disk._fd, new_payload, target * BLOCK_SIZE)
            t.join(timeout=5)
            self.assertFalse(t.is_alive())

            target_events = [e for e in events if e["index"] == target]
            self.assertEqual(len(target_events), 1)
            # 二次校验读到非零数据 → 跳过打洞，而不是抹掉新数据
            self.assertEqual(target_events[0]["state"], STATE_DATA)
            self.assertEqual(disk.read_block(target), new_payload)
            self.assertEqual(disk.block_state(target), STATE_DATA)

            # 其它零块不受影响，照常被打洞（8 个零块中除 target 外的 7 个）
            punched = sorted(e["index"] for e in events if e["state"] == "punched")
            self.assertNotIn(target, punched)
            self.assertEqual(len(punched), 7)
        finally:
            disk.close()

    def test_concurrent_writes_during_full_reclaim(self):
        """高压并发：回收进行中多个线程持续随机写，结束后盘上数据与最后写入一致。"""
        import random

        disk = self.open_disk(64)
        rng = random.Random(20261122)
        stop = threading.Event()
        errors = []

        def writer(seed):
            local = random.Random(seed)
            payloads = {}
            while not stop.is_set():
                idx = local.randrange(0, 64)
                kind = local.choice(["zero", "data"])
                if kind == "zero":
                    payload = b"\x00" * BLOCK_SIZE
                else:
                    payload = (f"w{seed}-b{idx}".encode() + b"\x77" * BLOCK_SIZE)[:BLOCK_SIZE]
                try:
                    disk.write_block(idx, payload)
                    payloads[idx] = payload
                except Exception as exc:  # pragma: no cover - 失败即测试失败
                    errors.append(exc)
            return payloads

        threads = [threading.Thread(target=writer, args=(s,)) for s in range(4)]
        # 线程内部各自记录最后写入，回收结束后逐块核对“最后写者”的值
        last_writes = {}
        originals = {i: disk.read_block(i) for i in range(64)}

        # 用包装写入记录全局最后值
        real_write = disk.write_block

        def recorded_write(index, data):
            last_writes[index] = bytes(data)
            return real_write(index, data)

        disk.write_block = recorded_write
        for t in threads:
            t.start()
        manager = ReclaimManager(disk)
        manager.start()
        # 等回收完成（写入仍在继续，可能出现新零块，下一轮可回收）
        while manager.state()["running"]:
            threading.Event().wait(0.005)
        stop.set()
        for t in threads:
            t.join()
        disk.write_block = real_write
        self.assertEqual(errors, [])

        for i in range(64):
            expected = last_writes.get(i, originals[i])
            self.assertEqual(disk.read_block(i), expected, f"块 {i} 数据与最后写入不一致")

        # 再跑一轮回收（无写入），应正常完成且不改变任何读值
        before = {i: disk.read_block(i) for i in range(64)}
        list(disk.reclaim())
        for i in range(64):
            self.assertEqual(disk.read_block(i), before[i])
    def test_hole_is_skipped_even_when_writer_creates_then_punches(self):
        """已有空洞在扫描中被 SEEK_DATA 整段跳过，不会触发无谓处理。"""
        disk = self.open_disk(16)
        try:
            events = list(disk.reclaim())
            touched = {e["index"] for e in events}
            # 预置空洞块（0..3、12..15）不应出现在事件里
            for hole in (0, 1, 2, 3, 12, 13, 14, 15):
                self.assertNotIn(hole, touched)
        finally:
            disk.close()


class TestInterruptionAndRestart(TempImageTestCase):
    def test_cancel_keeps_disk_and_holes_then_resume(self):
        disk = self.open_disk(256)  # 与生产默认一致：64 个零块
        try:
            capacity = disk.capacity
            zero_before = [i for i in range(256) if disk.block_state(i) == STATE_ZERO]
            self.assertEqual(len(zero_before), 64)

            # 注入每块处理延迟，制造稳定的中断窗口（不改变任何盘上语义）
            import time
            real_on = None

            def slow_reclaim(should_stop=None, on_event=None):
                def wrapped(ev):
                    time.sleep(0.002)
                    if on_event:
                        on_event(ev)
                yield from DiskImage.reclaim(disk, should_stop, wrapped)

            disk.reclaim = slow_reclaim
            manager = ReclaimManager(disk)
            manager.start()

            # 等至少打一个洞后请求中断
            state = manager.state()
            while state["running"] and state["punched_blocks"] == 0:
                threading.Event().wait(0.005)
                state = manager.state()
            manager.cancel()
            while manager.state()["running"]:
                threading.Event().wait(0.005)

            final = manager.state()
            self.assertTrue(final["finished"])
            self.assertTrue(final["cancel_requested"])
            self.assertGreaterEqual(final["punched_blocks"], 1)
            self.assertLess(final["punched_blocks"], len(zero_before))
            actual_after_cancel = disk.actual_bytes()

            # 已打的洞真实落盘：重开进程视角（新 DiskImage 实例）仍保留
            disk.close()
            disk2 = DiskImage(self.path, 256)
            holes_after = [i for i in range(256) if disk2.block_state(i) == STATE_HOLE]
            self.assertGreaterEqual(len(holes_after), 64 + final["punched_blocks"])
            self.assertEqual(os.path.getsize(self.path), capacity)

            # 重启后重新发起回收：跳过已有空洞，把剩余零块收完
            manager2 = ReclaimManager(disk2)
            manager2.start()
            while manager2.state()["running"]:
                threading.Event().wait(0.005)
            st2 = manager2.state()
            self.assertIsNone(st2["error"])
            remaining_zeros = [i for i in range(256) if disk2.block_state(i) == STATE_ZERO]
            self.assertEqual(remaining_zeros, [])
            # 两轮合计释放全部 64 个零块，只剩 64 个非零块占用空间
            self.assertEqual(disk2.actual_bytes(), 64 * BLOCK_SIZE)
            self.assertEqual(
                actual_after_cancel,
                (64 + 64 - final["punched_blocks"]) * BLOCK_SIZE,
            )
            disk2.close()
            disk = None  # 防止 tearDown 重复关闭
        finally:
            if disk is not None:
                disk.close()

    def test_restart_with_existing_fully_reclaimed_image(self):
        disk = self.open_disk(16)
        list(disk.reclaim())
        disk.close()
        # 模拟进程重启：镜像复用，重新回收为空操作
        again = create_image(self.path, 16)
        try:
            manager = ReclaimManager(again)
            manager.start()
            st = manager.state()
            while st["running"]:
                threading.Event().wait(0.005)
                st = manager.state()
            self.assertIsNone(st["error"])
            self.assertEqual(st["punched_blocks"], 0)
            # 只剩非零块占用空间
            self.assertEqual(again.actual_bytes(), 4 * BLOCK_SIZE)
        finally:
            again.close()


class TestApiCore(TempImageTestCase):
    def setUp(self):
        super().setUp()
        self.disk = self.open_disk(16)
        self.core = ApiCore(self.disk)

    def tearDown(self):
        self.disk.close()
        super().tearDown()

    def test_status_and_block_read(self):
        status = self.core.status()
        self.assertEqual(status["stats"]["block_count"], 16)
        self.assertEqual(len(status["blocks"]), 16)
        self.assertEqual(status["reclaim"]["running"], False)
        self.assertEqual(status["flush"]["pending_blocks"], 0)
        self.assertEqual(status["flush"]["running"], False)
        info = self.core.read_block(4)  # 16 块镜像：4..7 为写过的零块
        self.assertEqual(info["state"], STATE_ZERO)
        self.assertEqual(len(bytes.fromhex(info["hex"])), BLOCK_SIZE)

    def test_write_validation(self):
        with self.assertRaises(ApiError) as ctx:
            self.core.write_block(0, {"hex": "abcd"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError):
            self.core.write_block(0, {"hex": "zz"})
        with self.assertRaises(ApiError) as ctx:
            self.core.write_block(99, {"hex": (b"\x00" * BLOCK_SIZE).hex()})
        self.assertEqual(ctx.exception.status, 404)

    def test_write_then_read_roundtrip_and_reclaim_via_api(self):
        payload = (b"API-WRITE" + b"\x33" * BLOCK_SIZE)[:BLOCK_SIZE]
        self.core.write_block(0, {"hex": payload.hex()})
        # 写回缓存：读立即看到最新值，但 raw 镜像尚未变化
        self.assertEqual(bytes.fromhex(self.core.get_block(0)["hex"]), payload)
        self.assertEqual(self.core.status()["flush"]["pending_blocks"], 1)

        self.core.write_pattern(0, "zero")
        self.assertEqual(self.core.get_block(0)["state"], STATE_ZERO)

        # 回收只作用于 raw 镜像：先刷盘把零块落盘，回收才能把它打成洞
        self.core.flush()
        self.assertEqual(self.core.status()["flush"]["pending_blocks"], 0)
        before = self.core.status()["stats"]["actual_bytes"]
        self.core.reclaim_start()
        st = self.core.manager.state()
        while st["running"]:
            threading.Event().wait(0.005)
            st = self.core.manager.state()
        after = self.core.status()["stats"]["actual_bytes"]
        self.assertLess(after, before)
        self.assertEqual(self.core.get_block(0)["state"], STATE_HOLE)
        self.assertEqual(bytes.fromhex(self.core.get_block(0)["hex"]), b"\x00" * BLOCK_SIZE)

    def test_wsgi_routing_and_errors(self):
        app = create_wsgi_app(self.core)

        def call(method, path, body=None):
            req = {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "CONTENT_LENGTH": str(len(body)) if body else "0",
                "wsgi.input": io.BytesIO(body or b""),
            }
            captured = {}

            def start_response(status, headers):
                captured["status"] = status
                captured["headers"] = headers

            chunks = app(req, start_response)
            raw = b"".join(chunks)
            code = int(captured["status"].split(" ")[0])
            return code, json.loads(raw.decode())

        code, body = call("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["stats"]["block_count"], 16)

        code, body = call("GET", "/api/blocks/4")
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], STATE_ZERO)

        code, body = call("POST", "/api/blocks/1/write",
                          json.dumps({"hex": "ab"}).encode())
        self.assertEqual(code, 400)
        self.assertIn("4096", body["error"])

        code, body = call("GET", "/nope")
        self.assertEqual(code, 404)

        code, body = call("POST", "/api/blocks/2/write-pattern/zero")
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], STATE_ZERO)


if __name__ == "__main__":
    unittest.main(verbosity=2)
