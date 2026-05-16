FROM docker.m.daocloud.io/library/python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple grpcio grpcio-tools "protobuf>=6.31.1" "psycopg[binary]"
COPY proto/sms.proto /app/proto/sms.proto
RUN python -m grpc_tools.protoc -I /app/proto --python_out=. --grpc_python_out=. /app/proto/sms.proto
COPY herosms-sms-service/herosms_sms_server.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 50051
CMD ["python3", "herosms_sms_server.py"]
