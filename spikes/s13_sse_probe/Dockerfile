# Fallback for S6/S13 only if `nebius ai endpoint create` has no --inject-file.
# Build, push to the control registry and pass it as IMAGE_A (by digest) to s6_endpoint.sh.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
RUN useradd --uid 10001 --create-home p2p && mkdir -p /opt/p2p /data && chown p2p /data
COPY server.py /opt/p2p/server.py
USER 10001
EXPOSE 8080
ENTRYPOINT ["python", "/opt/p2p/server.py"]
