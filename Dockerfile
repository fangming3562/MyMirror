FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends nginx ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY deploy ./deploy
COPY mirror ./mirror
COPY private/fuclaude private/frame-shell.js ./private/
ENTRYPOINT ["python3", "/app/deploy/run.py", "--container", "--runtime", "/runtime"]
