# Base image pinned here on purpose. The Supervisor passes BUILD_FROM (HA's
# Alpine base, which has no Python), and the old build.yaml override is
# deprecated, so we ignore BUILD_FROM and use the official multi-arch Python
# image (arm64 for the HA Yellow, amd64 elsewhere).
FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY transit_board/ transit_board/
COPY default_config.yaml run.py ./

CMD ["python", "/app/run.py"]
