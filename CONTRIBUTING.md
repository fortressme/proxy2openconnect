# Contributing

感谢参与 proxy2openconnect。Bug 报告、文档修正和功能改进都欢迎提交。

## Before opening an issue

- 先搜索现有 Issue，确认问题尚未被报告。
- 使用最新版本复现，并阅读 README 的故障排查部分。
- 删除密码、OTP、Cookie、证书、VPN 地址、企业域名、公网 IP 和其他内部网络信息。
- 尽量提供最小配置与可重复步骤，不要上传完整的 `data/` 目录。

安全漏洞请按 [SECURITY.md](SECURITY.md) 私密报告，不要创建公开 Issue。

## Development

需要 Python 3.13、Docker 与 Docker Compose。克隆后运行：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
docker compose config --quiet
docker compose build
```

Shell 脚本应兼容 POSIX `sh`。提交前请确认没有生成文件、真实凭据或本地配置被加入版本控制。

## Pull requests

1. 从 `main` 创建主题分支。
2. 将变更限制在一个清晰目标内，并为行为变更补充测试和文档。
3. 更新 `CHANGELOG.md` 的 `Unreleased` 部分。
4. 确保持续集成全部通过。
5. 在 PR 中说明动机、验证方式、兼容性和安全影响。

提交 PR 即表示你同意以本项目的 MIT License 发布你的贡献。
