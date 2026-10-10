FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app/ ./

RUN apt-get update -y && \
    apt-get install -y --no-install-recommends rsync && \
    rm -rf /var/lib/apt/lists/* && \
    pip install . --no-cache-dir

CMD ["python3", "main.py"]