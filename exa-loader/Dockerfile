FROM python:3.12-slim

# Stdlib only -- no pip installs, no third-party deps.
WORKDIR /app
COPY app.py /app/app.py

RUN useradd -m -u 1000 loader
USER loader

EXPOSE 8080
CMD ["python", "-u", "/app/app.py"]
