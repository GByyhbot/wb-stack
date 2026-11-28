# ---- Stage 1: 前端构建（wb-web → wb-manager/static/dist，容器内产出，产物不入库）----
FROM node:20-alpine AS frontend-builder

WORKDIR /app/wb-web

# 先拷贝依赖清单再安装，充分利用 Docker 层缓存
COPY wb-web/package.json wb-web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY wb-web/ ./
# vue-tsc 严格类型检查 + vite build（outDir 指向 ../wb-manager/static/dist）
RUN npm run build

# ---- Stage 2: Python 运行时（不引入 Node 运行时）----
FROM python:3.11-slim

# 默认用国内镜像（NAS 侧构建可用）；ECS 侧构建时传 --build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

COPY wb-manager/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --timeout 60 --retries 5 \
    -i ${PIP_INDEX} -r /tmp/requirements.txt

COPY wb-manager/app /app/app
COPY wb-manager/static /app/static
COPY --from=frontend-builder /app/wb-manager/static/dist /app/wb-manager/static/dist
COPY wb-rotator/rotator.py /app/rotator.py
COPY wb-rotator/quota_engine.py /app/quota_engine.py
COPY deploy/entrypoint.py /app/entrypoint.py
COPY deploy/healthcheck.py /app/healthcheck.py

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    WB_DB_PATH=/data/manager/wb_manager.db \
    WB_ROTATOR_HOME=/data/rotator \
    WB_PORT_MANAGER=8300 \
    WB_PORT_ROTATOR=8310

EXPOSE 8300 8310
CMD ["python", "entrypoint.py"]
