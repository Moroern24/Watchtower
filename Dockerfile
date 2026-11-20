# Watchtower — API + dashboard on :8080
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
# lxml / pydantic ship manylinux wheels: no compiler needed.
# Chromium (+ its system libs) is only used while learning JavaScript portals.
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY watchtower ./watchtower
COPY mockportals ./mockportals

RUN useradd -m -u 10001 watch && mkdir -p /data && chown -R watch /data /ms-playwright
USER watch
VOLUME ["/data"]
EXPOSE 8080
CMD ["uvicorn", "watchtower.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
