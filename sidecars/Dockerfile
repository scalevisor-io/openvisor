# The dev agent's MCP sidecars (sidecars/app.py) - one image for all of them.
FROM python:3.12-slim

WORKDIR /srv
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx
COPY *.py ./

EXPOSE 3000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3000"]
