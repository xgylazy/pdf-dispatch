FROM python:3.11-slim AS base

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libgl1-mesa-glx libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY scheduler/ ./scheduler/
COPY worker/ ./worker/


# ---- 离线模型内嵌阶段（可选）----
# 用法：docker build --build-arg PREPARE_MODELS=1 --target=with-models .
# 仅在内网部署且希望"镜像即完整"时启用；
# 常规部署跳过此阶段，改为运行时挂载 OCR_MODEL_DIR 指向已下载目录。
FROM base AS with-models
ARG PREPARE_MODELS=0
# scripts/download_models.py 来自挂载的 pdf2tree 工程；构建前须通过
# --build-arg PDF2TREE_CONTEXT=../py_agent/pdf2tree 把工程放入构建上下文中。
COPY pdf2tree/scripts/download_models.py ./_dl_models.py
RUN if [ "$PREPARE_MODELS" = "1" ]; then \
        pip install --no-cache-dir "paddleocr>=2.7" "paddlepaddle>=2.6" && \
        python _dl_models.py --dest /app/model_cache/official_models --resume; \
    fi


FROM base AS release
# 默认不内嵌模型；运行时通过 OCR_MODEL_DIR 环境变量指向
# 外部已下载的目录（docker-compose 中用 volume 挂载）。
ENV PYTHONUNBUFFERED=1
