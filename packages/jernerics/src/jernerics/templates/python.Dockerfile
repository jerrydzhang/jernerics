FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install uv

WORKDIR /build/project
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --frozen --no-dev

RUN rm -rf /build /root/.cache

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/work/src
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

ENTRYPOINT ["python", "-m", "jernerics.runner"]
