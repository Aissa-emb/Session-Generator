FROM node:22-bookworm-slim AS ui
WORKDIR /build
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SG_DATA_DIR=/var/lib/session-generator SG_SECRET_DIR=/var/lib/session-generator/secrets SG_REQUIRE_AUTH=1 TMPDIR=/var/lib/session-generator
WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir pip==26.3 \
    && python -m pip install --no-cache-dir -r server/requirements.txt \
    && python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip \
    && groupadd --gid 10001 generator \
    && useradd --uid 10001 --gid 10001 --no-create-home generator \
    && mkdir -p /var/lib/session-generator/secrets \
    && chown -R 10001:10001 /var/lib/session-generator
COPY xcorgen/ xcorgen/
COPY server/ server/
COPY spec/ spec/
COPY docs/ docs/
COPY tools/ tools/
COPY generate.py ./
COPY --from=ui /build/dist/ apps/web/dist/
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Session Generator" \
      org.opencontainers.image.description="Continuous Web, Android and iOS demo telemetry with linked backend traces" \
      org.opencontainers.image.source="https://github.com/Aissa-emb/Session-Generator" \
      org.opencontainers.image.revision=$VCS_REF
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=3).read()"]
# Forward SIGTERM to the API; it coordinates worker drain. Tini also reaps
# orphaned grandchildren, including when Kubernetes supplies no init process.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "75"]
