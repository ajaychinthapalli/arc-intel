FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY arc_intel ./arc_intel
ARG EXTRAS=aws
RUN pip install --no-cache-dir --prefix=/install ".[${EXTRAS}]"

FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin arc-intel
COPY --from=build /install /usr/local
USER 10001
ENV PYTHONUNBUFFERED=1 PORT=8080 METRICS_PORT=9090
EXPOSE 8080 9090
ENTRYPOINT ["arc-intel"]
