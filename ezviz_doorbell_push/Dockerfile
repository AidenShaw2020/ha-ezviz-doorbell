# A fixed base image on purpose: the Supervisor only passes BUILD_FROM when a
# build.yaml pins a base per architecture, and an unset ARG makes the build
# fail with "base name should not be blank". python:*-alpine is multi-arch, so
# aarch64, amd64 and armv7 all resolve on their own.
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/requirements.txt

# pycryptodome (pulled in by pyezvizapi to decrypt snapshots) ships musllinux
# wheels for the common architectures but not all of them, so keep a toolchain
# around for the duration of the install in case it has to compile.
#
# ffmpeg is for the live view: the cloud stream arrives as MPEG-PS and has to
# be remuxed to MPEG-TS before Home Assistant's stream component will take it.
RUN apk add --no-cache ffmpeg \
 && apk add --no-cache --virtual .build-deps gcc musl-dev python3-dev \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && apk del .build-deps

COPY *.py /app/
WORKDIR /app

CMD ["python", "/app/ezviz_push.py"]
