FROM python:3.12-slim
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
# Unprivileged user. uid 1000 matches the Helm chart's securityContext, so a plain `docker run` is not root either.
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin oscilyx \
 && mkdir -p /data && chown 1000:1000 /data
WORKDIR /app
# Exact, tested versions (see requirements.lock) so two builds ship identical libraries.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY server.py integrations.py /app/
COPY static /app/static
ENV OSCILYX_HOST=0.0.0.0 OSCILYX_PORT=4318
EXPOSE 4318
VOLUME ["/data"]
ENV OSCILYX_DB=/data/oscilyx.db
USER 1000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('OSCILYX_PORT','4318'),timeout=3)"
CMD ["python", "-u", "server.py"]
