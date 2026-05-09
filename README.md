# NB Register

本项目用于本地跑账号注册、邮箱 OTP、GoPay 支付和工作流看板。

部分模块仅限授权研究、内部实验和非商用用途。支付和浏览器自动化相关实现参考并感谢 [DanOps-1/Gpt-Agreement-Payment](https://github.com/DanOps-1/Gpt-Agreement-Payment)。

根仓只保存 compose、文档和共享 proto。`account-db`、`browser-reg`、`dashboard`、`gopay-payment`、`orchestrator`、`outlook-imap-service` 等服务目录由各自仓库独立管理；旧 `whatsapp-relay` 不进入根仓。

## 1. 准备配置

```bash
cp compose.example.env compose.env
```

编辑 `compose.env`，至少确认这些值：

```env
OUTLOOK_EMAIL=
TOKEN_DIR=tokens
REGISTER_PROXY_URL=socks5://host.docker.internal:10813

PAYMENT_ADDR=gopay-payment:50051
OTP_ADDR=gopay-payment:50051

# GoPay payment service runtime config; not gRPC request parameters.
GOPAY_COUNTRY_CODE=62
GOPAY_PHONE_NUMBER=
GOPAY_PIN=
GOPAY_OTP_SERVICE_ADDR=gopay-payment:50051
GOPAY_OTP_TIMEOUT_SECONDS=60
GOPAY_WEBHOOK_PORT=8081
GOPAY_OTP_WEBHOOK_TOKEN=

GOPAY_PROXY_URL=socks5://host.docker.internal:10813
GOPAY_GRPC_PORT=50054
```

`compose.env`、token、日志、抓包和浏览器状态都不会入库。

## 2. 启动主服务

```bash
docker compose --env-file compose.env up -d --build
```

打开看板：

```text
http://127.0.0.1:8080
```

健康检查：

```bash
curl -fsS http://127.0.0.1:8080/api/health
```

## 3. Outlook 授权

`outlook-imap-service` 会读取：

```text
outlook-imap-service/tokens/outlook_refresh_token
```

如果 token 文件不存在、为空、过期或被撤销，服务会自动进入 Microsoft device flow。查看登录码：

```bash
docker compose --env-file compose.env logs -f outlook-imap-service
```

按日志提示打开 Microsoft device 页面，输入 code 授权即可。授权成功后 token 会写回 `outlook-imap-service/tokens/`，后续自动刷新。

强制重新授权：

```bash
rm -f outlook-imap-service/tokens/outlook_refresh_token
docker compose --env-file compose.env restart outlook-imap-service
docker compose --env-file compose.env logs -f outlook-imap-service
```

## 4. 支付和 GoPay OTP

主 compose 默认让 orchestrator 访问 GoPay payment 容器内服务：

```env
PAYMENT_ADDR=gopay-payment:50051
OTP_ADDR=gopay-payment:50051
```

GoPay payment 内置 OTP webhook。手机端通知转发工具把收到的 OTP POST 到：

```text
http://127.0.0.1:8081/webhook/otp
```

payload 可用 JSON 或纯文本，例如：

```bash
curl -X POST http://127.0.0.1:8081/webhook/otp \
  -H 'Content-Type: application/json' \
  -d '{"otp":"123456","source":"phone"}'
```

如果配置了 `GOPAY_OTP_WEBHOOK_TOKEN`，请求需要带：

```bash
-H "Authorization: Bearer $GOPAY_OTP_WEBHOOK_TOKEN"
```

GoPay payment 读取 `GOPAY_COUNTRY_CODE`、`GOPAY_PHONE_NUMBER`、`GOPAY_PIN` 和 `GOPAY_PROXY_URL`。如果要单独启动：

```bash
cd gopay-payment
set -a; source ../compose.env; set +a
./run_payment_server.sh
```

旧 `whatsapp-relay` 不再包含在主 compose 中。

## 5. 在看板操作

在 `http://127.0.0.1:8080`：

- 创建账号：可不填邮箱/密码，服务会生成 Outlook plus alias 和随机密码
- 注册账号：触发 browser-reg，等待 Outlook 邮件 OTP
- 激活账号：使用账号 session token / access token 触发 GoPay 支付，等待 GoPay OTP webhook 回传
- 注册并激活：按顺序执行注册和支付
- 账号详情：可查看/隐藏账号密码，可手动修改 session token
- 工作流详情：查看 job 状态、步骤、错误和结果摘要

账号有运行中的 job 时，行内操作会显示“进行中”并禁止重复触发。

## 常用命令

查看服务：

```bash
docker compose --env-file compose.env ps
```

查看日志：

```bash
docker compose --env-file compose.env logs -f orchestrator
docker compose --env-file compose.env logs -f browser-reg
docker compose --env-file compose.env logs -f outlook-imap-service
```

重启单个服务：

```bash
docker compose --env-file compose.env restart dashboard
```

重建单个服务：

```bash
docker compose --env-file compose.env up -d --build dashboard
```

停止：

```bash
docker compose --env-file compose.env down
```

## 开发检查

```bash
(cd account-db && go test ./...)
(cd orchestrator && go test ./...)
(cd dashboard && go test ./...)
(cd outlook-imap-service && go test ./...)
(cd dashboard/web && npm run build)
docker compose --env-file compose.env config --quiet
```

## 更多文档

- [account-db](account-db/README.md)
- [browser-reg](browser-reg/README.md)
- [dashboard](dashboard/README.md)
- [orchestrator](orchestrator/README.md)
- [outlook-imap-service](outlook-imap-service/README.md)
- [gopay-payment](gopay-payment/README.md)
