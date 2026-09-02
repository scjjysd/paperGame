FROM python:3.9-slim

# opencv-python 需要 libgl1/libglib2.0-0；Mesa 软渲染需要 libosmesa6；socat 供 entrypoint 转发
RUN apt-get update && apt-get install -y --no-install-recommends \
    libosmesa6 libosmesa6-dev libgl1 libglib2.0-0 socat curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/server

COPY requirements-service.txt .
RUN pip install --no-cache-dir -r requirements-service.txt

COPY vendor/AnimatedDrawings ./vendor/AnimatedDrawings
RUN pip install --no-cache-dir -e ./vendor/AnimatedDrawings --no-deps

COPY app ./app
COPY scripts ./scripts
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV RENDER_USE_MESA=true \
    OUT_ROOT=/data/out \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/server

VOLUME ["/data/out"]
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
