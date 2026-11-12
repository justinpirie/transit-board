ARG BUILD_FROM=python:3.13-slim
FROM ${BUILD_FROM}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY transit_board/ transit_board/
COPY default_config.yaml run.py ./

CMD ["python", "/app/run.py"]
