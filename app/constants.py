"""全局常量。"""
import os

# 模拟虚拟机的逻辑块大小：4 KiB
BLOCK_SIZE = 4096

# 预置 raw 镜像默认包含的 4 KiB 块数（256 * 4KiB = 1 MiB）
DEFAULT_BLOCK_COUNT = 256

# 回收线程每打多少个洞做一次 fsync，保证中断后已打的洞落盘
FSYNC_EVERY_PUNCHES = 32

# Linux fallocate(2) 相关常量，老版本 Python 的 os 模块可能缺失，做兜底
SEEK_DATA = getattr(os, "SEEK_DATA", 3)
SEEK_HOLE = getattr(os, "SEEK_HOLE", 4)

FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02

# 块状态
STATE_HOLE = "hole"        # 稀疏空洞，不占宿主盘空间，读为全零
STATE_ZERO = "zero"        # 已分配且内容全零（写过的零块，占空间、可回收）
STATE_DATA = "data"        # 已分配且含非零数据
