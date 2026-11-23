"""镜像初始化 / 应用入口命令。

* python -m app.cli init-image   创建预置 raw 镜像（幂等）
* python -m app.cli serve        无 Flask 环境下的 stdlib 回退服务
"""
from __future__ import annotations

import os

from .constants import DEFAULT_BLOCK_COUNT
from .disk import create_image


def main() -> None:
    import sys

    image_path = os.environ.get("IMAGE_PATH", "/data/disk.raw")
    block_count = int(os.environ.get("BLOCK_COUNT", str(DEFAULT_BLOCK_COUNT)))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "init-image"

    if cmd == "init-image":
        disk = create_image(image_path, block_count)
        snap = disk.stats()
        print(
            f"镜像就绪: {snap['path']} 容量={snap['capacity_bytes']}B "
            f"实占={snap['actual_bytes']}B 块数={snap['block_count']}"
        )
        disk.close()
    elif cmd == "serve":
        from .server_stdlib import main as serve

        serve()
    else:
        raise SystemExit(f"未知命令: {cmd}（支持 init-image / serve）")


if __name__ == "__main__":
    main()
