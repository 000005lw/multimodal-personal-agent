# OneBot v11 后端（SnowLuma）

本项目不实现 QQ 协议，只对接 OneBot v11。推荐用 docker 跑 SnowLuma。

```bash
docker run -d --name snowluma --restart unless-stopped \
  -p 127.0.0.1:3001:3001 \
  -e ONEBOT_ACCESS_TOKEN=<与 .env 的 ONEBOT_ACCESS_TOKEN 一致> \
  motricseven7/snowluma:latest
```

## 端口说明

| 端口 | 用途 | 是否对外 |
| --- | --- | --- |
| 3001 | OneBot HTTP API —— **我们发消息用这个** | 只绑 127.0.0.1 |
| 8090 | 我们自己的上报监听（`BOT_HOST=0.0.0.0`） | 容器通过 `172.17.0.1:8090` 访问宿主 |

在容器里访问宿主要用 docker 网桥地址（`172.17.0.1`），不能用 `127.0.0.1`——
这也是 `BOT_HOST` 必须是 `0.0.0.0` 的原因。

## 首次登录（扫码）

1. `docker logs -f snowluma` 里会给出二维码路径/链接；
2. 或者用本项目自带的邮件通道：给配置的邮箱发一封**主题含「扫码」**的邮件。

## 验证

```bash
curl -s http://127.0.0.1:3001/get_status -H "Authorization: Bearer <token>"
# {"status":"online", ...}
```
