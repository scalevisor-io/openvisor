FROM python:3.12-slim

WORKDIR /srv
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" asyncpg httpx
COPY main.py websearch.py egress_proxy.py .

EXPOSE 8100
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
