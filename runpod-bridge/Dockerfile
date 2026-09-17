FROM python:3.12-alpine

WORKDIR /app
RUN apk add --no-cache gcc musl-dev python3-dev libffi-dev openssl-dev
RUN pip install --no-cache-dir aiohttp==3.11.*

COPY app.py .
COPY workflow.json /data/workflow.json

ENV PORT=8190
EXPOSE 8190

CMD ["python", "app.py"]