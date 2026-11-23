#!/bin/sh
# 1) 幂等创建预置 raw 镜像（已存在则复用，保留原盘与已打的洞）
# 2) 以单进程多线程方式启动：回收在后台线程执行，块操作走块级锁
set -e

python -m app.cli init-image

exec gunicorn --bind "0.0.0.0:${PORT:-8000}" \
    --workers 1 \
    --threads 8 \
    --timeout 120 \
    "app.web:create_app()"
