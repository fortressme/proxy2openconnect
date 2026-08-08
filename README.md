# xray2cisco

通过 Docker 提供 **Xray 入站 → Cisco AnyConnect 兼容 VPN 出站** 的受控网络网关，并附带 Web 管理控制台。

客户端连接容器的 SOCKS5 或 HTTP 代理后，Xray 为所有可联网 outbound 设置 Linux `SO_MARK`。独立策略路由表只允许这些连接从 OpenConnect 创建的 `tun0` 离开；VPN 未连接、认证失败或异常退出时，出口保持 `unreachable`，不会自动回落到普通网络。

> 本项目使用开源 [OpenConnect](https://www.infradead.org/openconnect/) 实现 AnyConnect 协议兼容，不包含 Cisco 专有客户端，也不隶属于 Cisco。部署前请确认公司网络、安全和合规政策允许使用。

## 功能

- Xray SOCKS5 与 HTTP 入站，支持直接编辑标准 Xray JSON。
- Cisco AnyConnect 兼容 VPN：用户名、认证组、密码、OTP/MFA、DTLS、客户端证书和企业 CA。
- 自签名证书 TOFU：从失败日志提取 `pin-sha256`，由管理员确认后固定到当前网关。
- Windows AnyConnect 4.10.08029 默认身份：UA、`os=win` 与 version-string 保持一致。
- IPv4/IPv6 fail-closed 策略路由，VPN 断开时阻止 Xray 旁路泄漏。
- Web 登录、失败登录限流、安全响应头、HttpOnly 会话 Cookie。
- VPN/Xray 独立启停、健康状态和最近 500 行内存日志。
- Compose 持久化、可配置端口、Docker 日志轮转和优雅退出。
- `amd64`、`arm64`、`arm/v7` 多架构构建能力。

## 架构

```text
                         Web 管理端 :8000
                                │
客户端 ── SOCKS5 :1080 ─┐       │
                        ├──► Xray ── SO_MARK 255 ──► policy table 200
客户端 ─── HTTP :8080 ──┘                                  │
                                             ┌──────────────┴──────────────┐
                                          VPN 在线                     VPN 离线
                                             │                            │
                                          tun0 出口                 unreachable
                                             │
                                     Cisco ASA / VPN Gateway
```

Web 服务、OpenConnect 控制连接和容器自身流量不带 Xray mark，继续使用普通默认路由，因此不会形成 VPN 递归。

## 运行要求

- Linux Docker Engine 24+ 或兼容环境。
- Docker Compose v2。
- 宿主机存在 `/dev/net/tun`。
- 容器运行时允许 `NET_ADMIN` capability。
- 获得组织授权的 AnyConnect/ASA VPN 账号。

不需要也不应使用 `privileged: true`。

## 快速开始

### 1. 准备环境变量

```bash
cp .env.example .env
```

编辑 `.env`，取消前两项注释，并替换为独立的强随机值：

```dotenv
ADMIN_PASSWORD=<至少 12 字符的强密码>
SESSION_SECRET=<至少 32 字符的随机密钥>
```

可用 OpenSSL 生成：

```bash
openssl rand -base64 24
openssl rand -hex 32
```

若未设置这两项，Compose 会拒绝启动；后端也会再次检查最小长度。

### 2. 构建并启动

```bash
docker compose up -d --build
docker compose ps
```

默认只监听本机：

- Web 控制台：<http://127.0.0.1:8000>
- SOCKS5：`127.0.0.1:1080`
- HTTP Proxy：`127.0.0.1:8080`

Web 用户名默认为 `admin`，密码来自 `.env`。

### 3. 修改 Xray 默认密码

默认 SOCKS5/HTTP 账号仅用于首次设置：

```text
用户名：xray
密码：change-me
```

登录 Web 后进入“Xray 配置”，立即替换两处 `change-me`，保存并重启 Xray。

### 4. 配置 VPN

进入“VPN 配置”，至少填写：

- VPN 服务器，例如 `https://vpn.example.com`
- 用户名
- 认证组（网关要求时）
- 密码和可选 OTP

密码默认只用于本次连接。勾选“保存到数据卷”后才会持久化。

如果网关使用自签名证书，第一次连接会失败并显示证书指纹。确认指纹来源后点击“信任此证书指纹”，再重新连接。该功能只固定当前主机的公钥指纹，不会关闭全局证书校验。

### 5. 测试代理

VPN 状态显示“已连接”后执行：

```bash
curl --proxy socks5h://xray:<代理密码>@127.0.0.1:1080 https://ifconfig.me
curl --proxy http://xray:<代理密码>@127.0.0.1:8080 https://ifconfig.me
```

VPN 未连接时请求失败属于预期的 fail-closed 行为。

## 持久化

Compose 将宿主机 `./data` 挂载到容器 `/data`：

```text
data/
├── vpn/
│   └── config.json
├── xray/
│   └── config.json
└── certs/                 # 可选
```

容器重启、重建或执行 `docker compose down` 不会删除这些文件。

如果选择保存 VPN 密码，它会以权限 `0600` 的明文 JSON 存入 `data/vpn/config.json`。请把整个 `data/` 当作敏感数据处理，建议放在加密磁盘上。`data/` 已从 Git 和 Docker 构建上下文排除。

证书路径只允许位于 `/data/`，例如：

```text
/data/certs/client.p12
/data/certs/client.key
/data/certs/company-ca.pem
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `ADMIN_USERNAME` | `admin` | Web 管理用户名，当前在 Compose 中设置 |
| `ADMIN_PASSWORD` | 必填 | Web 管理密码，至少 12 字符 |
| `SESSION_SECRET` | 必填 | 会话签名密钥，至少 32 字符 |
| `BIND_ADDRESS` | `127.0.0.1` | 宿主机监听地址 |
| `WEB_PORT` | `8000` | Web 管理端宿主机端口 |
| `SOCKS_PORT` | `1080` | SOCKS5 宿主机端口，TCP/UDP |
| `HTTP_PROXY_PORT` | `8080` | HTTP Proxy 宿主机端口 |
| `TZ` | `UTC` | 容器时区 |
| `COOKIE_SECURE` | `false` | HTTPS 部署时必须设为 `true` |
| `APP_VERSION` | `0.1.0` | OCI 镜像与 API 版本 |
| `IMAGE_NAME` | `xray2cisco` | Compose 镜像名 |
| `IMAGE_TAG` | `0.1.0` | Compose 镜像标签 |

如需从其他主机访问，将 `BIND_ADDRESS` 改为指定宿主机地址。使用 `0.0.0.0` 前必须配置防火墙、强代理密码以及 Web HTTPS 反向代理。

## Xray 运行时规则

Web 页面保存的是原始标准 Xray JSON。进程启动前，后端会创建临时有效配置，并为每个非 `blackhole` outbound 注入：

```json
{
  "streamSettings": {
    "sockopt": {
      "mark": 255
    }
  }
}
```

原有 `streamSettings`/`sockopt` 会保留，但用户填写的 `mark` 会被强制覆盖。原始配置文件不会被这一过程改写。

默认 outbound 使用 IPv4。关闭“禁用 IPv6”且网关确实下发 IPv6 地址时，路由脚本会建立 IPv6 隧道路由；否则带 mark 的 IPv6 流量保持阻断。

## AnyConnect 身份

默认配置模拟 Windows AnyConnect 4.10.08029：

```text
User-Agent: AnyConnect Windows 4.10.08029
--os=win
--version-string=4.10.08029
```

AnyConnect 4.x 已结束生命周期，但部分旧 ASA/DAP 策略仍依赖 4.x 身份。可在 Web 中按实际网关策略修改。修改 UA 不等同于提供 Cisco HostScan、设备证明或其他专有能力。

## 安全基线

- 默认端口仅绑定 `127.0.0.1`。
- 不要提交 `.env`、`data/`、日志、OTP、VPN 地址或真实证书指纹。
- 对外提供 Web 管理端时使用 HTTPS，并设置 `COOKIE_SECURE=true`。
- 优先使用企业 CA；只在可信渠道核对后固定自签名证书。
- `extra_args` 使用允许列表，不能覆盖脚本、接口、凭据或证书安全边界。
- 备份和迁移 `data/` 时使用加密传输与存储。
- 容器仅申请 `NET_ADMIN` 和 `/dev/net/tun`，并启用 `no-new-privileges`。
- Docker 日志限制为单文件 10 MB、最多 3 个文件；Web 内存日志最多 500 行。

更多要求见 [SECURITY.md](SECURITY.md)。

## 日常运维

```bash
# 状态
docker compose ps

# 日志
docker compose logs -f --tail=200

# 停止但保留配置
docker compose down

# 升级并重建
docker compose build --pull
docker compose up -d

# 健康检查
curl http://127.0.0.1:${WEB_PORT:-8000}/health
```

备份：

```bash
tar -czf xray2cisco-data-$(date +%F).tar.gz data/
```

备份包可能包含明文 VPN 密码和私钥，必须加密保存。

## Windows / WSL2

推荐使用 Docker Desktop 的 WSL2 后端。若直接在 WSL 发行版中运行 `dockerd`，发行版休眠时 Windows 的 `127.0.0.1` 转发也会消失。请使用 systemd/Windows 启动任务管理 Docker 生命周期，或在测试期间保持一个 WSL 会话运行。

不要把 WSL 临时保活进程当作生产部署方案。

## 认证限制

以下策略通常不能只靠 OpenConnect 非交互模式完成：

- 必须打开系统浏览器的 SAML/SSO。
- Cisco Secure Desktop、HostScan 或复杂 DAP 终端合规检查。
- 需要反复选择字段的多阶段认证表单。
- 强制依赖 Cisco Secure Client 专有组件或系统证书存储。

遇到问题先查看 Web“实时日志”。不要把密码或 OTP 放入 `extra_args`。

## 故障排查

### `ERR_CONNECTION_REFUSED`

检查容器和端口：

```bash
docker compose ps
docker compose logs --tail=100
```

WSL 用户还应确认发行版和 Docker daemon 没有休眠。

### 证书显示 `signer not found`

优先让管理员提供企业 CA，并配置 `/data/certs/company-ca.pem`。临时自签名证书可通过 Web TOFU 卡片固定 `pin-sha256`，但必须先通过可信渠道核对。

### 一直显示“VPN 连接中”

查看 VPN 日志。如果出现 `Device for nexthop is not up`，说明使用了旧版路由脚本；升级到 0.1.0 或更高版本。当前版本会先配置地址、MTU 并启用 `tun0`，再安装策略路由。

### VPN 已连接但内部域名无法解析

项目不会覆盖容器全局 DNS。请在 Xray `dns` 配置中加入可经 VPN 访问的企业 DNS，或在 Compose 中提供适合环境的 DNS 配置。

### VPN 断开后代理无法访问互联网

这是预期行为。策略表保留 `unreachable default`，防止流量从普通网络泄漏。

### TUN 或权限错误

```bash
test -c /dev/net/tun && echo OK
```

确认 Compose 中保留 `/dev/net/tun` 和 `NET_ADMIN`。部分托管容器平台不允许这些能力，无法运行 VPN 网关。

## 开发与测试

核心单元测试只依赖 Python 标准库：

```bash
python -m unittest discover -s tests -v
```

CI 还会执行：

- Python/JSON 语法检查
- ShellCheck
- Docker 镜像构建

## 多架构构建与发布

生成不包含 `.env` 和 `data/` 的源码发布包：

```bash
sh scripts/package-release.sh
```

输出位于 `dist/xray2cisco-<version>.tar.gz`。脚本使用文件白名单，而不是对整个工作目录直接打包。

构建多架构镜像时，先创建并登录目标镜像仓库，然后执行：

```bash
VERSION=$(cat VERSION)
docker buildx build \
  --platform linux/amd64,linux/arm64,linux/arm/v7 \
  --build-arg APP_VERSION="$VERSION" \
  -t registry.example.com/xray2cisco:"$VERSION" \
  -t registry.example.com/xray2cisco:latest \
  --push .
```

基础镜像已在 Dockerfile 中固定到多架构 manifest digest。升级 Xray 或 Python 时，需要同时更新版本、digest、`VERSION` 和 [CHANGELOG.md](CHANGELOG.md)。

## 发布检查清单

1. 更新 `VERSION`、Compose 默认标签和 `CHANGELOG.md`。
2. 确认 `.env` 与 `data/` 未进入版本控制或构建上下文。
3. 搜索并删除真实 VPN 地址、用户名、密码、OTP 和证书指纹。
4. 运行单元测试、ShellCheck、Docker build 和健康检查。
5. 在隔离环境验证 VPN 连接、TUN 地址、IPv4/IPv6 fail-closed 和断线恢复。
6. 审查基础镜像 digest 与依赖安全公告。
7. 生成并发布多架构镜像，记录最终 manifest digest。
8. 公开发布前选择并添加项目级 `LICENSE`。

示例敏感信息检查：

```bash
git grep -nEi 'password|passwd|otp|pin-sha256|vpn\.' -- ':!README.md' ':!tests/**'
git status --ignored --short
```

## 版本与第三方组件

- xray2cisco：`0.1.0`
- Xray Core：`26.3.27`，镜像 digest 已固定
- OpenConnect：Debian 12 安全更新仓库版本
- Python：3.12 slim-bookworm，镜像 digest 已固定
- FastAPI：0.116.1
- Uvicorn：0.35.0

第三方许可证与商标声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。公开发布前需由项目所有者选择项目自身许可证。
