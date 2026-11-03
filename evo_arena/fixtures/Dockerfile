ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}
COPY server.py /srv/server.py
USER 10001:10001
EXPOSE 8800
ENTRYPOINT ["python3", "-u", "/srv/server.py"]
