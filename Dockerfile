FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# Non-root is nicer, but SSH identity mounts are often mode 600 owned by the
# deploying user; run as root inside the container so the key is readable.
# Harden by mounting only the needed identity + known_hosts read-only.

COPY scripts/healthcheck.sh /usr/local/bin/fleet-healthcheck
RUN chmod +x /usr/local/bin/fleet-healthcheck

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD ["/usr/local/bin/fleet-healthcheck"]

CMD ["python", "-m", "fleet_monitor"]
