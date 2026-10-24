# Build from this repository root on a native arm64 or amd64 builder:
# docker build --platform linux/arm64 -t samsung-tv-keybridge:local .
# Only the inert amd64 filesystem stage is foreign; it never executes a RUN.
FROM --platform=linux/amd64 python:3.12.12-bookworm@sha256:c0abd0758831ad99b7a29e0c1a875da9c4abb9a2e3f21e2eeb585dbcadfb6cd0 AS amd64-libs
FROM --platform=$BUILDPLATFORM python:3.12.12-bookworm@sha256:c0abd0758831ad99b7a29e0c1a875da9c4abb9a2e3f21e2eeb585dbcadfb6cd0 AS inputs
COPY packaging/fetch-build-inputs.sh /tmp/fetch-build-inputs.sh
RUN sh /tmp/fetch-build-inputs.sh
# Keep the x86 loader and its complete SDB dependency closure private. COPY
# dereferences the pinned image's symlinks; no amd64 executable runs here.
COPY --from=amd64-libs /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 /opt/keybridge/amd64/ld-linux-x86-64.so.2
COPY --from=amd64-libs /lib/x86_64-linux-gnu/libc.so.6 /lib/x86_64-linux-gnu/libm.so.6 /lib/x86_64-linux-gnu/libdl.so.2 /lib/x86_64-linux-gnu/libpthread.so.0 /lib/x86_64-linux-gnu/librt.so.1 /lib/x86_64-linux-gnu/libgcc_s.so.1 /usr/lib/x86_64-linux-gnu/libstdc++.so.6 /opt/keybridge/amd64/

FROM oven/bun:1.4.2@sha256:9114c058aeae42162ee16dd5084b95fe9473970bb6bcb5b232ab1630f0546895 AS bun
FROM ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 AS uv
FROM python:3.12.12-bookworm@sha256:c0abd0758831ad99b7a29e0c1a875da9c4abb9a2e3f21e2eeb585dbcadfb6cd0 AS build
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=inputs /opt/dotnet /opt/dotnet
COPY --from=inputs /opt/nuget-feed /opt/nuget-feed
ENV DOTNET_ROOT=/opt/dotnet DOTNET=/opt/dotnet/dotnet \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /app
COPY packaging/NuGet.Config /app/NuGet.Config
COPY upstream.lock.json /app/upstream.lock.json
COPY scripts/build-payloads.js /app/scripts/build-payloads.js
COPY payloads/remote/*.cs payloads/remote/*.csproj /app/payloads/remote/
RUN bun scripts/build-payloads.js
COPY controller/pyproject.toml controller/uv.lock /app/controller/
WORKDIR /app/controller
# Frozen, hash-verified wheels only; owned sources are copied, not installed
# through arbitrary build backends.
RUN uv sync --frozen --no-dev --no-build --no-install-project \
    --no-install-package samsung-tv-root
COPY controller/theatre /app/controller/theatre
RUN mkdir /opt/native-libs && cp /usr/lib/$(uname -m)-linux-gnu/libssl.so.3 \
    /usr/lib/$(uname -m)-linux-gnu/libcrypto.so.3 /opt/native-libs/

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS runtime
# Python and OpenSSL stay native; no host binfmt, apt install, or privileges.
COPY --from=build /usr/bin/openssl /usr/bin/openssl
COPY --from=build /opt/native-libs/ /usr/local/lib/
RUN ldconfig
COPY --from=build /etc/ssl/openssl.cnf /etc/ssl/openssl.cnf
COPY --from=inputs /opt/keybridge /opt/keybridge
COPY --chmod=0755 packaging/sdb /opt/keybridge/sdb
COPY --from=build /app/controller/.venv /app/controller/.venv
COPY --from=build /app/controller/theatre /app/controller/theatre
COPY --from=build /app/controller/.cache/upstream/src /app/upstream/src
COPY --from=build /app/controller/.cache/upstream/LICENSE /app/upstream/LICENSE
COPY --from=build /app/controller/build/payloads /opt/keybridge/payloads
COPY upstream.lock.json /opt/keybridge/upstream.lock.json
ENV PATH=/app/controller/.venv/bin:$PATH \
    PYTHONPATH=/app/controller:/app/upstream/src \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/data/home SDB=/opt/keybridge/sdb \
    SAMSUNG_TV_ROOT_COMMON_PAYLOAD_DIRECTORY=/opt/keybridge/payloads \
    SAMSUNG_TV_ROOT_COMMON31_PAYLOAD_DIRECTORY=/opt/keybridge/payloads
WORKDIR /opt/keybridge
# These do not start SDB's server or contact a TV. The HA entrypoint owns the
# explicit foreground `sdb fork-server server` lifecycle and local readiness.
RUN --network=none /opt/keybridge/sdb version \
    && python -c 'import theatre.ha_app; import samsung_tv_root.archive_root' \
    && openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out /tmp/crypto-check.pem \
    && openssl pkey -in /tmp/crypto-check.pem -check -noout \
    && rm /tmp/crypto-check.pem \
    && mkdir -p /data/home && chmod 0700 /data/home
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "theatre.ha_app"]
