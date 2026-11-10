FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/appuser
ENV GIT_AUTHOR_NAME=placement-optimiser GIT_AUTHOR_EMAIL=placement-optimiser@users.noreply.github.com GIT_COMMITTER_NAME=placement-optimiser GIT_COMMITTER_EMAIL=placement-optimiser@users.noreply.github.com
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser
COPY --chown=appuser . .
USER 10001
EXPOSE 8000
# ONE worker + threads: /metrics keeps its state in memory, so workers must not be duplicated.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4", "app:app"]
