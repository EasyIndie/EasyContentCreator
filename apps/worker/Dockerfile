FROM python:3.12-slim AS runtime
WORKDIR /app
COPY pyproject.toml* ./
COPY README.md* ./
COPY apps apps
COPY packages packages
COPY migrations migrations
RUN pip install --no-cache-dir .
CMD ["python", "-m", "apps.worker"]
