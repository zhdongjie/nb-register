# WhatsApp Forwarder

专用 Android 通知转发器，只监听 WhatsApp / WhatsApp Business，并把包含 OTP 的通知 POST 到本项目的 GoPay OTP webhook。

默认 webhook payload：

```json
{
  "otp": "123456",
  "source": "whatsapp"
}
```

服务端地址填：

```text
http://<本机局域网 IP>:8081/webhook/otp
```

## 构建

```bash
cd whatsapp-forwarder
./gradlew assembleDebug
```

也可以在构建时写入默认 webhook：

```bash
./gradlew assembleDebug -PdefaultWebhookUrl=http://192.168.1.10:8081/webhook/otp
```

APK 输出：

```text
whatsapp-forwarder/app/build/outputs/apk/debug/app-debug.apk
```

## 手机端设置

1. 安装 APK。
2. 打开应用，填写 webhook URL，保存。
3. 点击 `Open`，在系统通知访问设置里启用 `WhatsApp Forwarder`。
4. 在系统电池设置里取消后台限制；部分 ROM 还需要允许自启动。
5. 点击 `Test`，服务端 `gopay-payment` 日志应出现 OTP accepted。

## 为什么不会只发第一条

参考项目使用 `notificationKey` 做唯一索引，WhatsApp 同一会话可能复用同一个通知 key，第二条以后会被数据库 `IGNORE` 掉。这个专用版本：

- 队列表不对 `notificationKey` 加唯一约束；
- 每条候选 OTP 通知都生成独立 `eventId`；
- WorkManager 使用 `APPEND_OR_REPLACE` 追加队列处理，不会因为已有一次性任务在跑就丢掉后续触发；
- 会解析 `EXTRA_TEXT`、`EXTRA_BIG_TEXT`、`EXTRA_TEXT_LINES` 和 MessagingStyle messages，适配 WhatsApp 聚合通知。

实现参考了 ItsAzni/NotificationForwarder 的通知监听、Room 队列和 WorkManager 重试思路：https://github.com/ItsAzni/NotificationForwarder
