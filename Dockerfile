# Bağımlılık yok: yalnızca Python standart kütüphanesi. İmaj ~50 MB.
ARG BASE=python:3.12-alpine
FROM ${BASE}
WORKDIR /app
COPY nobetci ./nobetci
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65534
EXPOSE 8080
ENTRYPOINT ["python", "-m", "nobetci"]
