FROM python:3.12-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ipmitool \
    && rm -rf /var/lib/apt/lists/*
COPY noc_dashboard.py /app/noc_dashboard.py
COPY rack_store.py /app/rack_store.py
COPY discovery.py /app/discovery.py
COPY device_catalog.py /app/device_catalog.py
COPY hardware_catalog.json /app/hardware_catalog.json
COPY web /app/web
RUN mkdir /data && chown 65532:65532 /data && chmod 700 /data
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python", "-u", "/app/noc_dashboard.py"]
