# aicr webhook 服务镜像：零第三方依赖，镜像里只有代码本身
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY aicr/ aicr/
RUN pip install --no-cache-dir -e . \
    && useradd -r -u 1000 aicr \
    && mkdir -p /app/state && chown -R aicr:aicr /app

USER aicr
EXPOSE 8765
VOLUME ["/app/state"]

# sqlite 状态库放 /app/state（volume 持久化），健康检查供编排层探活
ENV AICR_DB=/app/state/aicr_state.db
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=2)"
CMD ["aicr", "serve", "--host", "0.0.0.0", "--port", "8765"]
