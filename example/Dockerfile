FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install uv

COPY pyproject.toml uv.lock README.md /build/project/
COPY src/ /build/project/src/

RUN cd /build/project \
    && export UV_PROJECT_ENVIRONMENT=/opt/venv \
    && uv sync --frozen --no-dev \
    && rm -rf /build /root/.cache

# Re-activate /opt/venv in login subshells: ENV sets PATH only at image build;
# tmux panes re-run /etc/profile and would otherwise drop /opt/venv/bin.
RUN printf '%s\n' \
      'if [ -d /opt/venv/bin ]; then' \
      '    case ":${PATH:-}:" in' \
      '        *:/opt/venv/bin:*) ;;' \
      '        *) export PATH="/opt/venv/bin:${PATH:-}" ;;' \
      '    esac' \
      '    export VIRTUAL_ENV=/opt/venv' \
      'fi' \
      > /etc/profile.d/jernerics.sh \
 && echo '. /etc/profile.d/jernerics.sh' >> /etc/bash.bashrc

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH="/work/src"
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

ENTRYPOINT []
