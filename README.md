# proxy2openconnect

[![CI](https://github.com/fortressme/proxy2openconnect/actions/workflows/ci.yml/badge.svg)](https://github.com/fortressme/proxy2openconnect/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

`proxy2openconnect` 是一个运行在 Docker 中的代理网关：客户端连接 Xray 提供的 SOCKS5 或 HTTP 代理，选中的出站流量再通过 OpenConnect 建立的 Cisco AnyConnect 兼容 VPN 隧道转发。项目同时提供一个用于配置 VPN、Xray 和查看运行日志的 Web 控制台。

> 本项目使用开源 OpenConnect 实现协议兼容，不包含 Cisco 专有客户端，与 Cisco Systems, Inc. 无隶属、授权或支持关系。使用前请确认你的组织政策允许第三方 VPN 客户端和代理转发。

## 功能

- SOCKS5 和 HTTP 代理入口，默认启用用户名/密码认证。
- Cisco AnyConnect 兼容 VPN，支持认证组、密码、OTP/MFA、DTLS、客户端证书和企业 CA。
- 三种路由模式：全部接管、使用 VPN 下发路由、手动 CIDR 网段。
- Xray outbound 标签级分流，只为指定 outbound 注入 Linux `SO_MARK`。
- VPN 断开时撤销策略路由并回落普通网络，不主动阻断代理流量。
- Web 登录保护、失败登录限流、安全响应头和 HttpOnly 会话 Cookie。
- 服务器证书公钥指纹确认、配置持久化和最近 500 行内存日志。
- `linux/amd64`、`linux/arm64`、`linux/arm/v7` 容器镜像。

## 流量模型

```text
客户端 ── SOCKS5 :1080 ─┐
                        ├──► Xray ── SO_MARK 255 ──► 策略路由表 200
客户端 ─── HTTP :8080 ──┘                                  │
                                             ┌──────────────┴──────────────┐
                                        当前模式包含的目标              其他目标
                                             │                            │
                                          tun0 / VPN                  普通网络
```

只有 `XRAY_VPN_OUTBOUND_TAGS` 选中的 Xray outbound 会被应用注入 mark。Web 控制台、OpenConnect 控制连接、容器自身流量和未选中的 outbound 继续使用普通路由，避免 VPN 控制连接递归进入隧道。

## 运行要求

- Linux Docker Engine 24+ 和 Docker Compose v2。
- 可用的 `/dev/net/tun`。
- 容器运行时允许 `NET_ADMIN` capability。
- 获得授权的 AnyConnect/ASA VPN 账号。

Windows 用户建议使用 Docker Desktop 的 WSL2 后端。部分托管容器平台不提供 TUN 或 `NET_ADMIN`，无法运行本项目。

## 快速开始

### 1. 获取项目

```bash
git clone https://github.com/fortressme/proxy2openconnect.git
cd proxy2openconnect
cp .env.example .env
```

编辑 `.env`，至少设置两个互不相同的随机值：

```dotenv
ADMIN_PASSWORD=replace-with-a-long-random-password
SESSION_SECRET=replace-with-at-least-32-random-characters
```

可以使用 OpenSSL 生成：

```bash
openssl rand -base64 24
openssl rand -hex 32
```

不要提交 `.env` 或 `data/`。

### 2. 启动

使用 GHCR 已发布镜像：

```bash
docker compose pull
docker compose up -d
```

从当前源代码构建：

```bash
docker compose up -d --build
```

默认端点：

| 服务 | 地址 |
|---|---|
| Web 控制台 | `http://127.0.0.1:8000` |
| SOCKS5 | `127.0.0.1:1080` |
| HTTP Proxy | `127.0.0.1:8080` |

Web 用户名默认为 `admin`，密码来自 `.env` 中的 `ADMIN_PASSWORD`。

### 3. 完成首次配置

1. 登录 Web 控制台。
2. 打开“Xray 配置”，把 SOCKS5 和 HTTP inbound 中的默认代理密码 `change-me` 替换为强密码。
3. 打开“VPN 配置”，填写服务器、用户名和需要的认证组。
4. 选择路由模式，保存后连接 VPN。
5. 如果网关证书不受系统 CA 信任，先通过可信渠道核对页面显示的公钥指纹，再选择信任。

VPN 密码默认只用于本次连接。只有勾选保存密码时，密码才会写入 `/data/vpn/config.json`。

## 路由模式

VPN 配置保存在 `/data/vpn/config.json`：

```json
{
  "route_mode": "all",
  "manual_routes": [],
  "manual_exclude_routes": []
}
```

### `all`：接管全部

默认模式。IPv4 安装 `0.0.0.0/0 → tun0`；VPN 提供 IPv6 地址时同时安装 `::/0 → tun0`。VPN 网关仍可能根据服务端策略拒绝访问某些目标。

### `vpn`：使用 VPN 下发路由

读取 OpenConnect 提供的 IPv4/IPv6 split include/exclude 信息：

- AnyConnect 中的 Secured Routes 进入 `tun0`。
- Non-Secured Routes 使用 `throw` 路由继续查询普通主路由。
- 更具体的 Secured Route 优先于较宽的 Non-Secured Route。

### `manual`：手动配置

在 Web 页面中每行填写一个 IPv4 或 IPv6 CIDR：

```text
10.0.0.0/8
192.168.50.10/32
fd00:1234::/48
```

`manual_routes` 中的目标进入 VPN，`manual_exclude_routes` 中更具体的目标回落普通网络。手动模式至少需要一个包含网段。

修改路由模式后，需要重新连接 VPN 才会应用到 OpenConnect 会话。

## Xray 出站选择

默认只为标签为 `vpn-out` 的 outbound 注入：

```json
{
  "streamSettings": {
    "sockopt": {
      "mark": 255
    }
  }
}
```

多个 VPN outbound 标签可在 `.env` 中用逗号分隔：

```dotenv
XRAY_VPN_OUTBOUND_TAGS=vpn-out,corporate-out
```

未选中的 outbound 保持原配置不变。原始 Xray JSON 不会因运行时注入而被改写。

## 验证代理和路由

```bash
curl --proxy socks5h://127.0.0.1:1080 \
  --proxy-user 'xray:YOUR_PROXY_PASSWORD' \
  https://ifconfig.me
```

查看最终路由策略：

```bash
docker compose exec proxy2openconnect cat /run/proxy2openconnect/split-routes
docker compose exec proxy2openconnect ip rule show
docker compose exec proxy2openconnect ip route show table 200
```

运行时路由文件格式示例：

```text
4 include 0.0.0.0/0
4 exclude 192.0.2.0/24
6 include fd00:1234::/48
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Web 管理用户名 |
| `ADMIN_PASSWORD` | 必填 | Web 管理密码，至少 12 个字符 |
| `SESSION_SECRET` | 必填 | 会话签名密钥，至少 32 个字符 |
| `BIND_ADDRESS` | `127.0.0.1` | 宿主机监听地址 |
| `WEB_PORT` | `8000` | Web 管理端口 |
| `SOCKS_PORT` | `1080` | SOCKS5 端口，TCP/UDP |
| `HTTP_PROXY_PORT` | `8080` | HTTP Proxy 端口 |
| `XRAY_VPN_OUTBOUND_TAGS` | `vpn-out` | 需要 VPN 策略的 outbound 标签 |
| `COOKIE_SECURE` | `false` | Web 使用 HTTPS 时设为 `true` |
| `TZ` | `UTC` | 容器时区 |
| `IMAGE_NAME` | GHCR 项目镜像 | Compose 镜像名 |
| `IMAGE_TAG` | `latest` | 镜像标签；生产环境建议固定版本 |

如需从其他主机访问，修改 `BIND_ADDRESS` 前必须配置主机防火墙、强代理密码和 Web HTTPS 反向代理。

## 数据与运维

持久化目录：

```text
data/
├── vpn/config.json
└── xray/config.json
```

常用命令：

```bash
docker compose ps
docker compose logs --tail=200
docker compose restart
docker compose down
```

Web 页面中的 OpenConnect/Xray 实时日志只保存在内存中，不会写入数据卷。备份 `data/` 时应使用加密存储，因为其中可能包含 VPN 密码、证书或私钥路径。

## 安全说明

- 默认端口仅绑定 `127.0.0.1`。
- 容器只申请 `/dev/net/tun` 和 `NET_ADMIN`，不需要 `privileged`。
- VPN 密码通过标准输入传给 OpenConnect，不出现在进程参数中。
- 证书和私钥路径只允许位于 `/data/`。
- VPN 断开后流量会回落普通网络；本项目默认不是 fail-closed 防泄漏边界。
- 本项目无法替代组织的设备合规、访问控制和审计策略。

完整安全策略与漏洞报告方式见 [SECURITY.md](SECURITY.md)。

## 已知限制

OpenConnect 非交互容器登录通常无法处理：

- 强制打开系统浏览器的 SAML/SSO。
- Cisco Secure Desktop、HostScan 或复杂 DAP 终端合规检查。
- 需要反复选择字段的多阶段交互认证。
- 强制依赖 Cisco Secure Client 专有组件或系统证书存储。

项目不会自动覆盖容器全局 DNS。访问企业内部域名时，请在 Xray DNS 配置中使用可经 VPN 到达的企业 DNS。

## 故障排查

### 容器或端口不可用

```bash
docker compose ps
docker compose logs --tail=100
curl http://127.0.0.1:8000/health
```

同时确认 `/dev/net/tun` 存在且 Compose 保留了 `NET_ADMIN`。

### VPN 已连接但目标没有进入隧道

检查 `/run/proxy2openconnect/split-routes` 和表 200。`all` 模式应至少出现 `4 include 0.0.0.0/0`；`vpn` 模式显示网关下发路由；`manual` 模式显示手动配置路由。

### 内部域名不能解析

先用内部 IP 验证路由，再检查 Xray DNS 配置和企业 DNS 地址是否包含在 VPN 路由中。

### 证书显示 `signer not found`

优先配置企业 CA。只有在通过可信渠道核对后，才固定页面显示的服务器公钥指纹。

## 开发与贡献

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -B -m unittest discover -s tests -v
sh -n entrypoint.sh scripts/*.sh
```

贡献流程和代码要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。版本历史见 [CHANGELOG.md](CHANGELOG.md)。维护者发布流程见 [RELEASING.md](RELEASING.md)。

## 许可证与致谢

本项目使用 [MIT License](LICENSE)。第三方组件及商标说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
