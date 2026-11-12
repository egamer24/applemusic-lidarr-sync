FROM python:3.13-alpine
WORKDIR /app
COPY ["app.py", "shortcut/Sync to Lidarr.shortcut", "./"]
RUN chmod 644 /app/* && mkdir /data && chown 1000:1000 /data
ENV DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
USER 1000:1000
HEALTHCHECK CMD wget -qO- http://localhost:8080/healthz || exit 1
CMD ["python", "app.py"]
