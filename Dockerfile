FROM python:3.12-alpine
COPY lava_provider_exporter.py /app/lava_provider_exporter.py
USER nobody
EXPOSE 9796
# listen on all interfaces inside the container; arguments passed to docker run are appended
ENTRYPOINT ["python3", "/app/lava_provider_exporter.py", "--listen", "0.0.0.0:9796"]
