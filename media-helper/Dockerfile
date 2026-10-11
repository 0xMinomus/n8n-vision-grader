FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 media \
    && useradd --uid 10001 --gid media --no-create-home --shell /usr/sbin/nologin media

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chown=10001:10001 server.py /app/server.py
COPY --chown=10001:10001 tests /app/tests
USER 10001:10001
EXPOSE 8080
CMD ["python3", "/app/server.py"]
