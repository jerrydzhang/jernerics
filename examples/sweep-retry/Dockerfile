FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install uv

COPY pyproject.toml uv.lock README.md /build/project/
COPY src/ /build/project/src/

RUN cd /build/project \
    && export UV_PROJECT_ENVIRONMENT=/opt/venv \
    && uv sync --frozen --no-dev \
    && rm -rf /build /root/.cache

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH="/work/src"
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

ENTRYPOINT []
