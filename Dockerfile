# Serving image for forge-pdm-mlops (F4).
#
# One slim image that installs the package with its [serve] extra and runs the FastAPI
# app over the *promoted* model. The MLflow backend (SQLite tracking DB + the model
# artifacts) is a mounted volume, not baked in — the same registry the training host
# writes to, so promoting/rolling back a version (F3) changes what this container serves
# with no rebuild. See docker-compose.yml for the serving + MLflow-UI two-service setup.

FROM python:3.12-slim

# LightGBM needs libgomp at runtime; nothing else system-level.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (pyproject only) so the layer caches across source edits.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[serve]"

# The canonical config + the offline smoke fixture travel with the image so the app is
# self-contained; the real registry is mounted at run time (see compose).
COPY configs ./configs
COPY data ./data

# The MLflow backend lives on a mounted volume; point the app's default paths at it.
ENV MLFLOW_TRACKING_URI="sqlite:////mlflow/mlflow.db"

EXPOSE 8000

# 0.0.0.0 so the port is reachable from outside the container.
CMD ["pdm", "serve", "--host", "0.0.0.0", "--port", "8000"]
