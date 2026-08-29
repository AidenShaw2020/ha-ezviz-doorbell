ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache python3 py3-pip

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY ezviz_push.py /app/ezviz_push.py
WORKDIR /app

CMD ["python3", "/app/ezviz_push.py"]
