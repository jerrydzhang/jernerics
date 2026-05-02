FROM python:3.12-slim

WORKDIR /work
COPY . .
RUN pip install uv && uv sync --no-dev --no-editable

ENV PYTHONPATH=/work/src
ENTRYPOINT ["python"]
