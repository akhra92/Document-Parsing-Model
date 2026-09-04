# DocumentAI HTTP API with LibreOffice, so Office inputs convert.
#
#   docker build -t documentai-api .
#   docker run --rm -p 8000:8000 documentai-api
#
# or `docker compose up` for the same with the settings in docker-compose.yml.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The same apt packages Streamlit Community Cloud installs, so the two
# deployments convert Office files identically.
COPY packages.txt /tmp/packages.txt
RUN apt-get update \
    && xargs -a /tmp/packages.txt apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /tmp/packages.txt

WORKDIR /app

# Install the package first so this layer only rebuilds when it changes.
COPY pyproject.toml README.md LICENSE ./
COPY documentai ./documentai
RUN pip install ".[api]"

COPY api.py ./

# LibreOffice needs a writable home for its profile; run as an unprivileged
# user rather than root.
RUN useradd --create-home --shell /usr/sbin/nologin documentai \
    && chown -R documentai:documentai /app
USER documentai
ENV HOME=/home/documentai

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
