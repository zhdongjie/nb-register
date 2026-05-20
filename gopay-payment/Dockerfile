ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ENV PIP_TRUSTED_HOST=mirrors.aliyun.com
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium

WORKDIR /app

RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i \
        -e 's|https\?://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' \
        -e 's|https\?://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
        /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
      sed -i \
        -e 's|https\?://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' \
        -e 's|https\?://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
        /etc/apt/sources.list; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium \
    && rm -rf /var/lib/apt/lists/*

COPY gopay-payment/gopay-flow/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY gopay-payment/gopay-flow/ /app/gopay-flow/
COPY proto/payment.proto /app/gopay-flow/proto/payment.proto
COPY gopay-payment/NOTICE gopay-payment/LICENSE gopay-payment/run_gopay.sh gopay-payment/run_payment_server.sh /app/

WORKDIR /app/gopay-flow
RUN python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. proto/payment.proto

EXPOSE 50051

ENTRYPOINT ["python", "payment_server.py"]
CMD ["--config", "config.json", "--listen", ":50051"]
