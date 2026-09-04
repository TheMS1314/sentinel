FROM python:3.12-slim

WORKDIR /app

# System deps: psycopg2-binary needs libpq at runtime (not just build
# time on some slim base images), and scikit-learn's wheel install is
# smoother with a C toolchain present for the rare case a wheel isn't
# available for this platform/Python combo.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# docker-compose.yml bind-mounts ./backend:/app over this for local
# dev (so code edits reload live via --reload below); this CMD is what
# actually runs in that mounted filesystem, and is also what runs
# as-is in any environment that builds this image without the
# dev-mode volume mount (e.g. a real deployment).
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
