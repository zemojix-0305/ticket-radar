FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，利用层缓存
COPY pyproject.toml README.md ./
COPY radar/ ./radar/
RUN pip install --no-cache-dir .

# 运行数据（SQLite）与配置
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 配置文件由外部挂载：docker run -v $PWD/tasks.yaml:/app/tasks.yaml ...
COPY tasks.example.yaml ./tasks.example.yaml

# 不给默认 CMD 的 --once，让它常驻；用 compose 或 docker run 覆盖
ENTRYPOINT ["radar"]
CMD ["run", "-c", "/app/tasks.yaml"]
