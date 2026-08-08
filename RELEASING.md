# Release guide

本文件供维护者发布新版本时使用。

## 1. Prepare

- 确认工作区只包含计划发布的变更。
- 更新 `VERSION`、`.env.example` 中的 `APP_VERSION` 和 `CHANGELOG.md`。
- 检查 README、许可证、第三方声明和安全策略。
- 确认仓库中没有 `.env`、`data/`、日志、证书、密钥、真实地址或账号信息。

## 2. Verify

```bash
docker compose config --quiet
docker compose build
sh scripts/package-release.sh
tar -tzf dist/proxy2openconnect-$(cat VERSION).tar.gz
```

发布包不包含 GitHub 配置、本地数据或开发缓存。至少执行一次容器启动、登录、VPN 连接和三种路由模式的人工冒烟测试。

## 3. Tag and publish

```bash
git tag -s "v$(cat VERSION)" -m "proxy2openconnect v$(cat VERSION)"
git push origin main "v$(cat VERSION)"
```

如果没有可用的签名密钥，可使用带说明的 annotated tag，但不要使用无说明的轻量标签。

推送 `v*` 标签后，GitHub Actions 会构建并发布 GHCR 多架构镜像。确认以下内容后再创建 GitHub Release：

- CI 与镜像发布工作流均成功；
- `ghcr.io/fortressme/proxy2openconnect:<version>` 可以拉取；
- Release 标题、变更摘要和附件版本一致；
- Release 附上 `proxy2openconnect-<version>.tar.gz` 及其 SHA-256 校验值。

## 4. Post-release

- 从干净环境按 README 完成一次安装验证。
- 将 `CHANGELOG.md` 的比较链接推进到下一版本。
- 对已修复的 Issue/PR 添加版本信息并关闭。
