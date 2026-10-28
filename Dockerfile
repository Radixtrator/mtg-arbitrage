FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MTG_HOST=0.0.0.0 \
    MTG_PORT=8765

WORKDIR /app
COPY . .

# Price downloads and user data (buy list, history) live in these two volumes.
VOLUME ["/app/.cache", "/app/data"]
EXPOSE 8765

CMD ["python3", "app.py"]
