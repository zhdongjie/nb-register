# NB Register

本项目用于本地编排账号注册、Outlook 邮件 OTP、GoPay 支付和工作流看板。

> 使用本项目即表示接受 [NOTICE.md](NOTICE.md)。本项目仅限授权研究、内部实验、协议分析、CTF/安全研究和教学验证，严禁商业化运营、账号批量生产或转售、代注册、代充值、规避支付、欺诈、未授权自动化，或任何违反第三方服务条款及适用法律法规的活动。

支付和浏览器自动化相关实现参考并感谢 [DanOps-1/Gpt-Agreement-Payment](https://github.com/DanOps-1/Gpt-Agreement-Payment)。Outlook 邮箱注册流程的实现思路源自 [LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister)。

## 快速启动

```bash
cp -n .env.example .env
```

Docker Compose 会自动读取项目根目录的 `.env` 做变量插值。编辑 `.env` 顶部的用户配置项，通常只需要改这些：

```env
REGISTER_PROXY_URL=socks5://host.docker.internal:10813

OUTLOOK_REGISTER_PROXY=socks5://host.docker.internal:10810
# 可选：多个 Outlook 注册/OAuth 代理，设置后会覆盖 OUTLOOK_REGISTER_PROXY。
OUTLOOK_REGISTER_PROXY_POOL=

GOPAY_COUNTRY_CODE=62
GOPAY_PHONE_NUMBER=
GOPAY_PIN=
GOPAY_PROXY_URL=socks5://host.docker.internal:10813
GOPAY_SIGNUP_AUTH_UUID=
```

启动：

```bash
docker compose build camoufox-base
docker compose up -d --build
```

打开看板：

```text
http://127.0.0.1:8080
```

健康检查：

```bash
curl -fsS http://127.0.0.1:8080/api/health
```

## 使用流程

推荐按这个顺序使用：

1. 配好代理和 GoPay 参数，启动整套 compose。
2. 在看板「邮箱注册」里注册 Outlook 邮箱；注册 job 成功后会启动一个独立 OAuth job。也可以在「邮箱管理」里手动导入已有 Outlook 邮箱和密码。
3. 如果自动 OAuth job 失败，或导入的是已有邮箱，在「邮箱管理」里点击单个邮箱 OAuth，或点击「补 OAuth」批量补齐 refresh token。
4. 完成 OAuth 的邮箱会进入账号注册取邮箱池。
5. 在「账号」页创建/注册账号；注册流程会从邮箱服务领取可用邮箱并等待 OTP。
6. 需要支付时，在账号详情里补 session token / access token，再触发 GoPay 激活。

## 配置说明

`.env.example` 已按使用频率分层：

- `User settings`：首次运行必须确认，包含账号注册代理、Outlook 注册/OAuth 代理池、GoPay 手机号/PIN/代理。
- `Optional host ports`：默认即可，只有本机端口冲突时再改。
- `Stable defaults`：高级默认值，正常不要改。

真实值只写入本地 `.env`。`.env` 会被 Git 忽略；`.env.example` 只保留可提交的空值和安全默认值，用于同步新增配置项。

## Outlook 邮件服务

`outlook-imap-service` 负责邮箱池、OAuth token 刷新和按需收信取 OTP。邮箱管理和 OpenAI 账号注册是分开的：邮箱页面只管理 Outlook 邮箱，账号注册流程只从邮箱池领取可用邮箱。

每个可用于注册取码的 Outlook 邮箱都需要完成 Microsoft OAuth。可以通过看板「邮箱注册」自动注册 Outlook 邮箱；注册成功后会按 `OUTLOOK_REGISTER_ENABLE_OAUTH2=true` 启动独立 OAuth job。这个动作是 best-effort side effect，失败或超时不会影响邮箱注册导入，邮箱会保持 `OAUTH_PENDING`，之后可在「邮箱管理」里点击「补 OAuth」补齐 token。也可以在看板「邮箱管理」里手动添加已有 Outlook 邮箱和密码，再点击「补 OAuth」自动补齐 token。

注册流程等待 OTP 时会自动从 Outlook 拉取近期邮件。缺少 OAuth 的邮箱不会进入注册取码池。

### Outlook 邮箱注册

`outlook-register-service` 的 Outlook 注册思路源自 [LainsNL/OutlookRegister](https://github.com/LainsNL/OutlookRegister)。日常使用直接在看板「邮箱注册」里触发；已有邮箱则在「邮箱管理」里导入后执行 OAuth。

先在 `.env` 填写 Outlook 注册参数。常用项如下：

```env
OUTLOOK_REGISTER_PROXY=socks5://host.docker.internal:10810
# 可选：多个 Outlook 注册/OAuth 代理，设置后会覆盖 OUTLOOK_REGISTER_PROXY。
# 支持逗号、空格或换行分隔；也可以用 OUTLOOK_REGISTER_PROXY_FILE 指向容器内文件。
OUTLOOK_REGISTER_PROXY_POOL=
OUTLOOK_REGISTER_PROXY_FILE=
# 注册成功后自动尝试 OAuth；失败不影响邮箱注册导入。
OUTLOOK_REGISTER_ENABLE_OAUTH2=true
OUTLOOK_REGISTER_OAUTH_TIMEOUT_SECONDS=90
```

其他 Outlook 注册参数通常保持 `.env.example` 默认值即可。

Outlook 注册和 OAuth 对代理质量比较敏感，推荐使用代理池：

- `OUTLOOK_REGISTER_PROXY`：单代理 fallback，保持向后兼容。
- `OUTLOOK_REGISTER_PROXY_POOL`：内联代理池，支持逗号、空格或换行分隔，例如 `socks5://host.docker.internal:10810,socks5://host.docker.internal:10814`。
- `OUTLOOK_REGISTER_PROXY_FILE`：文件代理池，文件位于容器内；常用路径是 `/app/Results/proxies.txt`，对应宿主机 `outlook-register-service/register-results/proxies.txt`。
- 如果设置了代理池，注册和 OAuth 每次动作会轮换取一个代理；未设置代理池时才使用 `OUTLOOK_REGISTER_PROXY`。

日常推荐直接用看板「邮箱注册」按钮。如果没有新账号，注册动作会返回失败，不会把空结果当成功。注册器默认同一时间只跑一个注册进程；重复触发会直接返回锁错误。

邮箱前缀会用 Python `Faker("en_US")` 生成英文名/姓并追加数字后缀，例如 `adamdiaz4168@outlook.com`，避免纯随机字母串。

验证码默认走自动流程；遇到当前脚本不能处理的验证码类型、风控页或代理质量问题时，注册会失败并在 `outlook-register-service/register-results/` 留下截图和日志，换代理后重新触发即可。

看板「邮箱注册」用于自动注册 Outlook 邮箱并导入邮箱池，并为新邮箱启动 best-effort OAuth job；看板「邮箱管理」里的 OAuth 按钮用于补跑或手动触发 OAuth，自动登录微软并换取 refresh token。dashboard 不挂 Docker socket，也不执行宿主机命令。

如果 Microsoft 要求验证备用邮箱，可以在 `outlook-register-service/register-results/` 写入 `oauth_proof_email_<email>.txt` 和 `oauth_code_<email>.txt`，分别填完整备用邮箱和验证码；也可用 `oauth_proof_email.txt` / `oauth_code.txt` 作为临时全局输入。

注册过程日志：

```bash
docker compose logs -f dashboard
docker compose logs -f orchestrator
tail -f outlook-register-service/register-results/register.log
```

## GoPay OTP

`whatsapp-otp-relay` 统一接收 WhatsApp OTP webhook，并缓存给编排服务通过 gRPC 消费。手机端通知转发工具默认把收到的 GoPay OTP POST 到：

```text
http://192.168.0.115:8081/webhook/otp
```

仓库内置了一个专用 Android 转发器：

```bash
cd whatsapp-forwarder
./gradlew assembleDebug
```

本地安装 `whatsapp-forwarder/app/build/outputs/apk/debug/app-debug.apk`，或从 GitHub Releases 下载 `whatsapp-forwarder.apk`。安装后确认 webhook URL，并在系统设置中启用 `WhatsApp Forwarder` 通知访问。

本机测试：

```bash
curl -X POST http://127.0.0.1:8081/webhook/otp \
  -H 'Content-Type: application/json' \
  -d '{"otp":"123456","source":"whatsapp"}'
```

也支持纯文本 payload。

GoPay 支付参数在 `.env` 中配置：

```env
GOPAY_COUNTRY_CODE=62
GOPAY_PHONE_NUMBER=
GOPAY_PIN=
GOPAY_PROXY_URL=socks5://host.docker.internal:10813
GOPAY_SIGNUP_AUTH_UUID=
```

## CheckPhone Telegram Bot

`checkphone-tgbot` 是独立容器，不调用 `gopay-cycle`。它复制了一份号码检测逻辑，通过 Telegram Bot API 长轮询 `getUpdates` 接收消息，再用 `sendMessage` 回复检测结果，所以不需要开放 webhook 端口。

在本地 `.env` 填写：

```env
TELEGRAM_BOT_TOKEN=
# 可选：只允许这些 chat id 使用，逗号/空格/换行分隔；留空表示不限制。
TELEGRAM_ALLOWED_CHAT_IDS=
# 可选：Telegram API 代理，GoPay 检测仍使用 GOPAY_PROXY_URL。
TELEGRAM_PROXY=
```

启动：

```bash
docker compose --profile tgbot up -d --build checkphone-tgbot
```

Bot 支持：

```text
1. 发送 /check-gopay-registered
2. 按提示发送手机号
```

手机号支持 `628xxxxxxxxxx`、`8xxxxxxxxxx`、`+62 8xxxxxxxxxx` 这几类格式。手机号必须作为命令后的下一条消息发送。

Telegram 命令菜单本身只允许小写字母、数字和下划线，不能显示 `/check-gopay-registered`，所以菜单只保留 `/help` 说明项。

私有 GoPay 登录命令只允许 `TELEGRAM_OWNER_CHAT_IDS` 中的 chat id 使用。流程：

```text
/login-gopay
按提示发送手机号
按提示发送 PIN
如需二次验证，按提示发送 OTP
```

登录成功后 bot 会检查 GoPay Wallet 余额是否大于 `GOPAY_REQUIRED_BALANCE_RP`，默认是 `1` Rp。token 只保存到容器内 `/data/gopay_login_state.json`，不会通过 Telegram 返回。可用 `/gopay-status` 重新检查已保存账号余额，用 `/clear-gopay-login` 清空本地登录状态。

## 看板操作

在 `http://127.0.0.1:8080` 可以执行：

- 创建账号：可不填邮箱/密码；邮箱会从邮箱池领取，密码会随机生成。
- 邮箱注册：自动注册 Outlook 邮箱并导入邮箱池。
- 邮箱管理：查看邮箱状态，手动导入已有 Outlook 邮箱，或对缺 token 的邮箱执行 OAuth。
- 注册账号：触发 `browser-reg`，默认最多等待 180 秒获取 Outlook 邮件 OTP；如果邮箱服务没取到码，可以在「工作流详情」对运行中的注册 job 手动提交 OTP。
- 激活账号：使用账号 session token / access token 触发 GoPay 支付，等待 `whatsapp-otp-relay` 回传 GoPay OTP。
- 注册并激活：按顺序执行注册和支付。
- 账号详情：查看/隐藏账号密码，修改 session token。
- 工作流详情：查看 job 状态、步骤、错误和结果摘要。

账号有运行中的 job 时，行内操作会显示“进行中”并禁止重复触发。

## 常用命令

查看服务：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f orchestrator
docker compose logs -f browser-reg
docker compose logs -f gopay-payment
docker compose logs -f whatsapp-otp-relay
docker compose logs -f outlook-imap-service
```

重启单个服务：

```bash
docker compose restart dashboard
```

重建单个服务：

```bash
docker compose up -d --build dashboard
```

停止：

```bash
docker compose down
```

## 开发检查

```bash
./scripts/generate-proto.sh
(cd account-db && go test ./...)
(cd orchestrator && go test ./...)
(cd dashboard && go test ./...)
(cd outlook-imap-service && go test ./...)
(cd outlook-register-service && python3 -m py_compile register_service.py register_provider.py camoufox_register.py)
(cd dashboard/web && npm run build)
docker compose --env-file .env.example config --quiet
```

## 赞赏

<img src="assets/zan.jpg" alt="赞赏码" width="240">
