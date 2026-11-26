# syntax=docker/dockerfile:1.7
FROM node:24.22.3-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS web
WORKDIR /build
COPY web/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv
FROM denoland/deno:bin-2.9.8@sha256:b507583d3c68ba69f3ed2ce5969ae5b8b70f43421c781279c7aea06435263543 AS deno
FROM joeanamier/xhs-downloader:2.8@sha256:5878c0972cc1236cd8ac4e069f9202152434d8402df52bf427210229b8a53fb9 AS xhs-runtime

FROM scratch AS xhs-source
ADD --checksum=sha256:d7d8901e1a8f7264f9774455cf593c2aca3b4c80dbf3361fcd9ffc0d7fb431af https://codeload.github.com/JoeanAmier/XHS-Downloader/tar.gz/47840a1bee8438324ff10753c4291148c46071c8 /xhs-2.8-source.tar.gz

FROM python:3.14.10-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=deno /deno /usr/local/bin/deno
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends ffmpeg tini ca-certificates libgomp1 \
    && dpkg-query -W ffmpeg libavcodec59 libavformat59 > /usr/local/share/reelucid-system-versions.txt \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 reelucid && useradd --uid 10001 --gid reelucid --create-home reelucid
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" REELUCID_DATA_DIR=/data REELUCID_MODELS_DIR=/data/models \
    REELUCID_HOST=0.0.0.0 REELUCID_PORT=8000 REELUCID_WEB_DIR=/app/web/dist \
    TMPDIR=/data/work/tmp TEMP=/data/work/tmp TMP=/data/work/tmp \
    HF_HOME=/data/models/.cache DENO_DIR=/data/work/deno XDG_CACHE_HOME=/data/work/cache PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv UV_CACHE_DIR=/root/.cache/uv uv sync --locked --extra server --no-dev --no-install-project

FROM dependencies AS small-model
RUN --mount=type=bind,source=docker/models,target=/seed,readonly \
    --mount=type=cache,target=/var/cache/reelucid-models,sharing=locked \
    HF_HOME=/var/cache/reelucid-models HF_HUB_DISABLE_IMPLICIT_TOKEN=1 TMPDIR=/tmp \
    python -I /seed/preinstall.py /opt/reelucid-models/small

FROM dependencies AS runtime
LABEL org.opencontainers.image.version="1.1.3"
COPY --from=small-model /opt/reelucid-models/small /opt/reelucid-models/small
COPY README.md LICENSE ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv UV_CACHE_DIR=/root/.cache/uv uv sync --locked --extra server --no-dev --no-editable
COPY --from=web /build/dist /app/web/dist
COPY docker/reelucid-deno /usr/local/bin/reelucid-deno
RUN chmod 755 /usr/local/bin/reelucid-deno \
    && mkdir -p /data/work/tmp /data/models && chown -R 10001:10001 /data
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=6s --start-period=30s --retries=3 CMD ["reelucid", "healthcheck"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["reelucid", "serve"]

FROM runtime AS with-xhs
USER 0:0
COPY --from=xhs-runtime /usr/local/ /opt/reelucid-xhs/python/
COPY --from=xhs-source --chmod=0444 /xhs-2.8-source.tar.gz /opt/reelucid-xhs/xhs-2.8-source.tar.gz
COPY --chmod=0555 docker/xhs/resolve /opt/reelucid-xhs/resolve
COPY docker/xhs/resolve.py /opt/reelucid-xhs/resolve.py
RUN dpkg-query -W libseccomp2 > /usr/local/share/reelucid-xhs-system-versions.txt \
    && mkdir -p /opt/reelucid-xhs/upstream/Volume && tar -xzf /opt/reelucid-xhs/xhs-2.8-source.tar.gz \
    --strip-components=1 -C /opt/reelucid-xhs/upstream --wildcards '*/source' '*/locale' '*/static' '*/LICENSE'
USER 10001:10001
RUN --network=none --mount=type=bind,source=docker/xhs/verify.py,target=/tmp/verify-xhs.py,readonly python -I /tmp/verify-xhs.py

FROM runtime AS default
