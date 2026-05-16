FROM docker.m.daocloud.io/library/python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple "requests[socks]" grpcio grpcio-tools
COPY checkphone-tgbot/bot.py checkphone-tgbot/checkphone.py checkphone-tgbot/orchestrator_client.py ./
COPY proto/email.proto proto/orchestrator.proto /app/proto/
RUN python -m grpc_tools.protoc -I /app/proto --python_out=. --grpc_python_out=. /app/proto/email.proto /app/proto/orchestrator.proto
ENV PYTHONUNBUFFERED=1
CMD ["python3", "bot.py"]
