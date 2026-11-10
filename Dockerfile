FROM python:3.12-slim
# PUID/PGID: the user the server runs as (Unraid's nobody:users by default).
# FORWARDED_ALLOW_IPS: proxies trusted to report the visitor's address (used for sign-in throttling).
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data TMPDIR=/data/tmp PORT=8686 \
    PUID=99 PGID=100 FORWARDED_ALLOW_IPS=*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh
EXPOSE 8686
VOLUME ["/data"]
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
