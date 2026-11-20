# Dedicated, hardened image for the outbound pinned browser proxy.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

WORKDIR /app

# Install minimal runtime requirements for url_security & browser_proxy
RUN pip install --no-cache-dir "httpcore>=1.0.0" "httpx>=0.28.1" \
    && useradd --create-home --shell /usr/sbin/nologin --uid 1000 proxyuser

COPY src/ /app/src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

USER 1000:1000

EXPOSE 8080

ENTRYPOINT ["python", "-m", "agent_system.services.browser_proxy"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
