# syntax=docker/dockerfile:1
#
# imgfastler — Docker Desktop extension.
#
# A single image packages all three extension components:
#   * ui/        -> frontend (static files, extracted by Docker Desktop)
#   * backend/   -> Python service (runs inside the Desktop VM)
#   * metadata.json + docker-compose.yaml -> metafile / service definition
#
# Build:  docker build --tag imgfastler/imgfastler:latest .
# Install: docker extension install imgfastler/imgfastler:latest

########################################################################
# 1. Backend dependencies (copied from the local venv, no PyPI access) #
########################################################################
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Create the venv using this image's Python so shebangs/pyvenv.cfg match.
RUN python -m venv /opt/venv

# Copy pre-installed dependencies from the local venv. PyPI is unreachable in
# some build environments, so we reuse the already-installed site-packages
# instead of running `pip install -r requirements.txt`.
COPY .venv/lib/python3.12/site-packages /opt/venv/lib/python3.12/site-packages

########################################################################
# 2. Extension image                                                   #
########################################################################
FROM python:3.12-slim

LABEL org.opencontainers.image.title="imgfastler" \
      org.opencontainers.image.description="Browse local images and start a container from any of them, with mounts, networks, environment variables and ports." \
      org.opencontainers.image.vendor="imgfastler" \
      com.docker.desktop.extension.api.version=">= 0.3.0" \
      com.docker.desktop.extension.icon="https://www.docker.com/wp-content/uploads/2022/03/Moby-logo.png" \
      com.docker.extension.screenshots="" \
      com.docker.extension.detailed-description="<h1>imgfastler</h1><p>List the images available to Docker Desktop, pick one and create a container from it. While creating you can configure bind mounts and volumes, network attachments, environment variables and port mappings.</p>" \
      com.docker.extension.publisher-url="" \
      com.docker.extension.additional-urls="" \
      com.docker.extension.changelog="<ul><li>Initial release: image browser, container creation with mounts, networks, env vars and ports.</li></ul>" \
      com.docker.extension.categories="utility-tools"

# Metafile and icon at the root of the image (required by Docker Desktop).
COPY metadata.json .
COPY docker-compose.yaml .
COPY docker.svg .

# Frontend, served from the path declared in metadata.json (ui.root).
COPY ui ./ui

# Backend service.
COPY backend/server.py /opt/imgfastler/server.py
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EXTENSION_SOCKET=/run/guest-services/imgfastler.sock

# Docker Desktop connects to this socket (metadata.json: vm.exposes.socket).
# For development with TEST_PORT set, it runs over TCP instead.
CMD ["python", "/opt/imgfastler/server.py"]
