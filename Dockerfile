# renovate: datasource=docker depName=python
ARG PYTHON_VERSION=3.9.4
FROM --platform=${BUILDPLATFORM} python:${PYTHON_VERSION} AS deps

RUN pip install poetry
COPY pyproject.toml poetry.lock ./
RUN poetry export --without-hashes -f requirements.txt >/tmp/requirements.txt

FROM python:${PYTHON_VERSION}

RUN apt-get update && apt-get install -y \
    libguestfs-dev \
    qemu-user-static

COPY --from=deps /tmp/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app
COPY . .

WORKDIR /build

ENTRYPOINT [ "/app/build.py" ]
