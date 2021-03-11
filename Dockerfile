FROM python:3.9.2 AS base

FROM base AS deps

RUN pip install poetry
COPY pyproject.toml poetry.lock ./
RUN poetry export --without-hashes -f requirements.txt >/tmp/requirements.txt

FROM base

RUN apt-get update && apt-get install -y \
    libguestfs-dev \
    qemu-user-static

COPY --from=deps /tmp/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app
COPY . .

WORKDIR /build

ENTRYPOINT [ "/app/build.py" ]
