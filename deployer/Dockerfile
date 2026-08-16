FROM python:3.12-alpine

COPY requirements.txt /srv/requirements.txt
RUN apk add --no-cache docker-cli \
    && pip install --no-cache-dir -r /srv/requirements.txt

WORKDIR /srv
COPY main.py k8s.py healthcheck.py programs_common.py browsershot.py ./

EXPOSE 8500
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8500"]
