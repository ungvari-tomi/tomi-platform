FROM mongo:8.0@sha256:bdc216e9d5a6ec2cfdac789b5832fdab64cd32055f59885cae918a92de63c361
RUN mkdir -p /opt/catalog /backups && chown 999:999 /backups && chmod 700 /backups
COPY contract.json /opt/catalog/contract.json
COPY init.js /docker-entrypoint-initdb.d/10-catalog.js
COPY health.js backup.js /opt/catalog/
ENV HOME=/tmp MONGOSH_DISABLE_TELEMETRY=1
USER 999:999
CMD ["mongod", "--auth", "--bind_ip_all", "--wiredTigerCacheSizeGB=0.25", "--setParameter", "enableLocalhostAuthBypass=false"]
