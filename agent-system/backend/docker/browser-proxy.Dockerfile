# Dedicated, minimal image for the outbound pinned browser proxy.
FROM python:3.12-slim

WORKDIR /app

# Install minimal runtime requirements for url_security & browser_proxy
RUN pip install --no-cache-dir httpcore>=1.0.0 httpx>=0.28.1

COPY src/ /app/src/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["python", "-m", "agent_system.services.browser_proxy"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
