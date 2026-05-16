# 环境规则

- 远程 Kubernetes 部署必须使用仓库脚本 `scripts/deploy-remote.sh`，不要手写临时 Helm/kubectl 部署命令作为主路径。
- `nb-register` 容器镜像构建、导入和 Helm 升级在远程宿主机 `pood1e@192.168.0.126` 上通过部署脚本完成；Mac 本机不作为容器构建或远程部署运行环境。
- 前端 `npm run dev` 只允许用于本地预览，不能作为远程环境部署或验证步骤。
