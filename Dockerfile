FROM python:3.12-slim

# System deps for asyncpg (C extension) and psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Alembic config must be at working directory root
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.bot"]
