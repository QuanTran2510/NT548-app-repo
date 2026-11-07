FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser
COPY --chown=appuser . .
USER 10001
EXPOSE 8000
# ONE worker + threads: /metrics keeps its state in memory, so workers must not be duplicated.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4", "app:app"]
