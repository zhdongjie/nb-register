# NB Register IaC

此目录承载 Kubernetes 安装变量和 Helm chart。业务 `.env` 不直接用于集群安装；集群变量统一写到 Helm values。

## 目录

```text
iac/
  helm/nb-register/          # 主 Helm chart
    values.yaml              # 默认变量，非生产密钥仅作占位
    values.local.example.yaml
```

## 变量分层

- `configEnv`：非敏感运行参数，渲染为 ConfigMap。
- `secrets.stringData`：敏感参数，渲染为 Secret。
- `workloads`：每个服务的镜像、端口、探针、挂载和副本数。
- `ingress`：dashboard 和 WhatsApp OTP webhook 的外部入口。

`host.docker.internal` 不适用于 Kubernetes。代理地址要改成集群可达的 Service、内网 IP 或 egress proxy。

## 使用

```bash
cp iac/helm/nb-register/values.local.example.yaml iac/helm/nb-register/values.local.yaml
```

编辑 `values.local.yaml` 后验证：

```bash
helm version --short
helm lint iac/helm/nb-register -f iac/helm/nb-register/values.local.yaml
helm template nb-register iac/helm/nb-register \
  --namespace nb-register \
  -f iac/helm/nb-register/values.local.yaml \
  >/tmp/nb-register.yaml
```

安装或升级：

```bash
helm upgrade --install nb-register iac/helm/nb-register \
  --namespace nb-register \
  --create-namespace \
  --rollback-on-failure \
  --wait=watcher \
  --wait-for-jobs \
  --timeout 10m \
  -f iac/helm/nb-register/values.local.yaml
```

验证：

```bash
helm status nb-register -n nb-register
kubectl -n nb-register get pods,svc,pvc
kubectl -n nb-register get events --sort-by=.lastTimestamp
```
