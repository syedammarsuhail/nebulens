FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
# Unprivileged user. uid 1000 matches the Helm chart's securityContext, so a plain `docker run` is not root either.
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin nebulens \
 && mkdir -p /data && chown 1000:1000 /data
WORKDIR /app
# Exact, tested versions (see requirements.lock) so two builds ship identical libraries.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY server.py integrations.py /app/
COPY static /app/static
ENV NEBULENS_HOST=0.0.0.0 NEBULENS_PORT=4318
EXPOSE 4318
VOLUME ["/data"]
ENV NEBULENS_DB=/data/nebulens.db
USER 1000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('NEBULENS_PORT','4318'),timeout=3)"
CMD ["python", "-u", "server.py"]
