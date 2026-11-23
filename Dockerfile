FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IMAGE_PATH=/data/disk.raw \
    BLOCK_COUNT=256 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 数据卷：raw 镜像存放处，必须是支持 fallocate PUNCH_HOLE 的文件系统
# （ext4 / xfs / overlay2 backed by ext4 均可；某些网络文件系统不支持）
VOLUME ["/data"]

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
