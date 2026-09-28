FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY debug_page.html .
COPY gunicorn.conf.py .

# Create data directory for SQLite volume mount
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "main:app"]
