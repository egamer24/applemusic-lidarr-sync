FROM python:3.13-alpine
WORKDIR /app
COPY ["app.py", "shortcut/Sync to Lidarr.shortcut", "shortcut/Sync to Lidarr (albums).shortcut", "./"]
RUN chmod 644 /app/* && mkdir /data && chown 1000:1000 /data
# starts as root only to hand /data to PUID:PGID, then drops privileges (see drop_privileges)
ENV DATA_DIR=/data PORT=8080 PUID=1000 PGID=1000 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
# 127.0.0.1, not localhost: localhost can resolve to ::1 and the server listens on IPv4
HEALTHCHECK CMD wget -qO- -T 5 http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "app.py"]
