# Query-plane container for the ECS Fargate MCP services (hub + per-repo).
#
# Contract: linux/arm64, MCP streamable-http on 0.0.0.0:8000 at POST /mcp,
# stateless. graphify's serve path needs only networkx (+ the mcp extra); the
# ~28 tree-sitter grammars are build-plane-only, so we install graphifyy with
# --no-deps and add the serving deps explicitly. Versions are pinned to the
# combination verified against graphifyy 0.9.51.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Pull the base image's Debian security updates (Trivy: openssl fix).
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

# pip is removed after installing: nothing installs at runtime, and its vendored
# copies (msgpack/setuptools SBOM) are what image scanners flag.
RUN pip install --no-cache-dir --no-deps graphifyy==0.9.51 \
 && pip install --no-cache-dir \
      "networkx==3.6.1" \
      "numpy==2.5.2" \
      "rapidfuzz==3.14.5" \
      "mcp==2.1.1" \
      "starlette==1.6.0" \
      "uvicorn==0.52.4" \
      "boto3==1.43.86" \
 && pip uninstall -y pip

COPY entrypoint.py /app/entrypoint.py

RUN useradd --create-home --uid 1000 graphify \
 && mkdir -p /graphs && chown graphify:graphify /graphs
USER graphify

# GRAPHIFY_API_KEY must never be set here: graphify's api-key middleware also
# matches "Authorization: Bearer" and would 401 proxied requests.
# GRAPHIFY_OUT must stay unset (relative default): an absolute value collapses
# every project_path onto one graph via pathlib's absolute-path join.
ENV PYTHONUNBUFFERED=1 GRAPHS_ROOT=/graphs

EXPOSE 8000
CMD ["python", "/app/entrypoint.py"]
