FROM python:3.13-alpine
WORKDIR /app
COPY ["app.py", "shortcut/Sync to Lidarr.shortcut", "./"]
RUN chmod 644 /app/* && mkdir /data && chown 1000:1000 /data
# starts as root only to hand /data to PUID:PGID, then drops privileges (see drop_privileges)
ENV DATA_DIR=/data PORT=8080 PUID=1000 PGID=1000 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK CMD wget -qO- http://localhost:8080/healthz || exit 1
CMD ["python", "app.py"]
