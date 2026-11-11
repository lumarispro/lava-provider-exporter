FROM python:3.12-alpine
COPY lava_provider_exporter.py /app/lava_provider_exporter.py
USER nobody
EXPOSE 9796
ENTRYPOINT ["python3", "/app/lava_provider_exporter.py"]
CMD ["--listen", "0.0.0.0:9796"]
