# The CI Observatory web app (docs/web-app.md). It needs no GitHub CLI: analyses call the REST API directly.
#   docker build -t ci-observatory .
#   docker run --rm -p 8080:8080 -e CIO_PUBLIC_URL=http://localhost:8080 -e CIO_SERVER_TOKEN=... ci-observatory
FROM ghcr.io/astral-sh/uv:0.12.6@sha256:88bc6eb1ccd4b82efd0e1b530caffabddf50dc2bf612e66c14ea25b8ee8a4d3d AS uv

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --locked --no-dev --no-editable --extra server \
    && rm -rf src
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data
USER app
ENV PATH=/app/.venv/bin:$PATH HOST=0.0.0.0 PORT=8080 CIO_DATA_DIR=/data CI_OBSERVATORY_TRANSPORT=http
EXPOSE 8080
CMD ["python", "-m", "ci_observatory.server"]
