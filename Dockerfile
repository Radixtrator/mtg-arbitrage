FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MTG_HOST=0.0.0.0 \
    MTG_PORT=8765 \
    PUID=99 \
    PGID=100

WORKDIR /app
COPY . .
RUN chmod +x docker-entrypoint.sh

# Price downloads and user data (buy list, history) live in these two volumes.
VOLUME ["/app/.cache", "/app/data"]
EXPOSE 8765

ENTRYPOINT ["./docker-entrypoint.sh"]
