# 迁移到新服务器（REDEPLOY）

> 旧机：腾讯云轻量 2G（`106.55.12.208`），**2026-11-23 中午 12:00 到期关机**。
> 本文目标：在新机器上用 30–60 分钟把一模一样的状态跑起来。
> 顺序很重要：**先备份 → 再部署 → 最后切流量**。

---

## 0. 迁移清单（哪些是"不可再生"的）

| 类别 | 内容 | 丢了会怎样 | 在哪 |
| --- | --- | --- | --- |
| **数据**（最重要） | `data/*.json`、`data/neuron.db`、`data/emb_cache.json`、`data/songs/`、`data/stickers/`、`data/voice/` | 记忆、好感、关系、曲库全没 | 备份包 |
| **配置** | `.env`（4 个 key、token、好友名单、SMTP、BILI cookie） | 起不来 | 备份包 |
| **代码** | 仓库 main 分支 | 可重新 clone | GitHub |
| **登录态**（可选） | `/opt/snowluma/config/QQ/nt_qq_*/nt_db`（约 8 MB） | 需重新扫码（1 分钟） | 备份包 |
| **系统集成** | `whale-bot.service`（systemd user unit） | 开机不自启 | 备份包 |
| **依赖清单** | `pip freeze` | 版本漂移 | 备份包 |

**可以丢**：`data/singing/`（待发音频）、`data/frames/`（视频抽帧）、`data/*.bak*`（历史备份）、`__pycache__`。

---

## 1. 在旧机上打包（已完成，命令留档）

```bash
cd /home/ubuntu/whale-bot
sudo tar czf /tmp/whale-data.tar.gz \
  --exclude='data/singing' --exclude='data/frames' \
  --exclude='data/*.bak*' --exclude='data/*.leakbak*' \
  --exclude='data/*.autoreplybak*' --exclude='data/stickers.bak_*' \
  --exclude='__pycache__' \
  data .env

# 登录态（只留 nt_db，跳过 380MB 媒体缓存）
sudo tar czf /tmp/whale-qq-session.tar.gz \
  --exclude='config/QQ/nt_qq_*/nt_data' --exclude='config/QQ/nt_qq_*/nt_temp' \
  -C /opt snowluma/config/QQ

cp /home/ubuntu/.config/systemd/user/whale-bot.service /tmp/
.venv/bin/pip freeze > /tmp/pip-freeze.txt
```

拉回本地（在你电脑上）：

```powershell
scp ubuntu@<OLD_IP>:/tmp/whale-data.tar.gz .
scp ubuntu@<OLD_IP>:/tmp/whale-qq-session.tar.gz .
scp ubuntu@<OLD_IP>:/tmp/whale-bot.service .
scp ubuntu@<OLD_IP>:/tmp/pip-freeze.txt .
```

---

## 2. 新服务器准备

推荐同规格：**Ubuntu 22.04 / 2 核 2G / 40G 盘**（数据 400MB，够用）。

```bash
# 2.1 基础
sudo apt update && sudo apt install -y python3-venv python3-pip docker.io curl git
sudo usermod -aG docker $USER && newgrp docker   # 免 sudo 用 docker

# 2.2 代码
git clone https://github.com/000005lw/multimodal-personal-agent.git ~/whale-bot
cd ~/whale-bot

# 2.3 虚拟环境（版本对齐旧机：Python 3.10.x）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 若 pip-freeze.txt 里有额外版本，按它装：
# .venv/bin/pip install -r pip-freeze.txt
```

**关键**：`requirements.txt` 是宽松的，若要完全复刻旧机版本（aiohttp 3.14.5 / httpx 0.28.1 /
numpy 2.2.6 / sherpa_onnx 2.0.0），用 `pip-freeze.txt`。

---

## 3. 恢复数据与配置

```bash
# 3.1 代码就位后，把 data 与 .env 解回去（会在 ~/whale-bot 下覆盖）
tar xzf whale-data.tar.gz -C ~/whale-bot
ls -la ~/whale-bot/data | head       # 应该有 neuron.db / memory.json / songs/
cat ~/whale-bot/.env | head -3       # 确认有内容（别外传）

# 3.2 权限（tar 还原后属主可能不对）
sudo chown -R $USER:$USER ~/whale-bot

# 3.3 登录态（可选；不做的话新机器扫码一次）
sudo mkdir -p /opt/snowluma
sudo tar xzf whale-qq-session.tar.gz -C /opt/snowluma
sudo chown -R 1000:1000 /opt/snowluma/config 2>/dev/null || true
```

---

## 4. 起 QQ 桥接（OneBot v11）

```bash
# 4.1 用旧机同款镜像与端口（3001 只绑本机）
sudo docker run -d --name snowluma --restart unless-stopped \
  -p 127.0.0.1:3001:3001 -p 127.0.0.1:5099:5099 -p 127.0.0.1:6081:6081 \
  -v /opt/snowluma/data:/app/data \
  -v /opt/snowluma/config:/app/.config \
  -v /opt/snowluma/local:/app/.local/share \
  -e TZ=Asia/Shanghai \
  motricseven7/snowluma:latest

# 4.2 看是否已登录（登录态迁过来就不用扫码）
sudo docker logs -f snowluma
```

- **如果显示已登录**：直接下一步。
- **如果要扫码**：二维码在容器日志里；或者用 bot 的邮件通道
  （给 `MAIL_TO` 发一封主题含「扫码」的邮件，bot 会把二维码发回来）。

> ⚠️ 登录态不一定能吃住（QQ 换 IP 有时会强制重登）。**扫码是兜底，不要怕**。

---

## 5. 起 bot

```bash
# 5.1 systemd user service
mkdir -p ~/.config/systemd/user
cp whale-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now whale-bot

# 5.2 关键：没有它，退出 SSH 后服务会被杀
sudo loginctl enable-linger $USER

# 5.3 看日志
journalctl --user -u whale-bot -f
```

**验证启动成功**的标志（日志里应该有）：

```
whale ｜ 监听 http://0.0.0.0:8090 ｜ 模型 deepseek-chat
whale ｜ NapCat 已连通（或 SnowLuma 已连通）
```

---

## 6. 迁移后验证清单

```bash
# ① 自测（离线，不花钱）
cd ~/whale-bot && .venv/bin/python selfcheck.py     # 期望 123/123

# ② 桥接连通
curl -s http://127.0.0.1:3001/get_status -H "Authorization: Bearer <ONEBOT_ACCESS_TOKEN>"

# ③ 上报口在听（本机 curl 应 200）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/health

# ④ 端口与权限
ss -lntp | grep -E '8090|3001'

# ⑤ 数据完整
.venv/bin/python - <<'PY'
import json, pathlib
d = pathlib.Path('data')
print('neuron.db', (d/'neuron.db').stat().st_size//1024, 'KB')
print('memory 会话数', len(json.load(open(d/'memory.json'))['conversations']))
print('曲库', len(json.load(open(d/'songs/index.json'))))
PY
```

**人工验证（QQ 里）**：

- [ ] 主人发 `/指令` 有回应
- [ ] `/记忆` 能看到旧记忆（说明 neuron.db 迁移成功）
- [ ] `/查好感` 名单和分数与旧机一致
- [ ] `/事件` 里的定时还在
- [ ] 白名单外的人发消息**完全无回应**
- [ ] 朋友号发消息，回复里**不出现「爸爸」**

---

## 7. 家里中继（如果继续用唱歌/看视频）

新机 IP 变了，家里那台电脑要改一行：

```powershell
# 桌面\小鲸鱼工具\whale-relay.py（或 whale-bot\home_agent.py）
SERVER = "ubuntu@<NEW_IP>"
```

然后重启中继窗口（`小鲸鱼刷B站.bat`）。**记得把本机 SSH 公钥加到新服务器**：

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh ubuntu@<NEW_IP> "cat >> ~/.ssh/authorized_keys"
```

---

## 8. 云厂商侧配置

| 项 | 设置 |
| --- | --- |
| 安全组入站 | 只放 **22**（SSH）。**8090 不要对公网开放**（旧机就是靠这个把 `/jobs/*` 和上报口挡住） |
| 出站 | 全部放行（要访问 DeepSeek / 阿里云 / B 站） |
| 防火墙（ufw） | 若开了 ufw，`sudo ufw allow 22/tcp` 即可；8090 不需要开 |
| 时区 | `sudo timedatectl set-timezone Asia/Shanghai`（定时功能依赖本地时间） |

---

## 9. 切换顺序（保数据不丢）

1. **旧机仍在跑时**：完成第 1 步打包（已做）。
2. **旧机停机前 10 分钟**：再打一次增量（只需 `data/`，因为数据一直在变）：
   ```bash
   systemctl --user stop whale-bot
   tar czf /tmp/whale-data-final.tar.gz -C /home/ubuntu/whale-bot data .env
   scp /tmp/whale-data-final.tar.gz <你电脑>
   ```
   **先停服务再打包**，否则 sqlite 可能拷到写一半的库。
3. **新机部署 + 验证**（第 2–6 步）。
4. **关闭旧机**（到期自动关，或手动）。
5. 若短期两台同时在跑：**只让一台连 OneBot**，否则同一条消息回两遍。

---

## 10. 排障

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 服务起不来，日志报 `.env` | `.env` 没解开或权限不对 | `ls -la .env`，`chmod 600 .env` |
| 日志说监听 8090 但 QQ 不回 | 容器访问不到宿主 | `.env` 里 `BOT_HOST=0.0.0.0`（不能 127.0.0.1） |
| 容器里上报失败 | SnowLuma 的上报地址还是旧 IP | 进容器 WebUI 改上报地址为 `http://172.17.0.1:8090/` |
| 定时全乱 | 时区不是 Asia/Shanghai | `timedatectl set-timezone Asia/Shanghai` + 重启服务 |
| 记忆是空的 | `neuron.db` 没解压对 | 检查 `data/neuron.db` 大小（约 3.8 MB） |
| 唱歌说"家里电脑没连着" | 中继还连着旧 IP | 改 `home_agent.py` 的 SERVER 并重启中继 |
| SSH 退出后 bot 停 | 没开 linger | `sudo loginctl enable-linger $USER` |

---

## 11. 备份包内容对照

    whale-deploy-<date>.zip
    ├── whale-bot/           代码（= 仓库 main，含 .env）
    ├── data-backup/         tar 解开后的 data（或直接放 tar）
    ├── deploy/              systemd unit + 本文件 + snowluma 说明
    ├── pip-freeze.txt       精确依赖版本
    └── docs/                全部文档

**这个 zip 含密钥与真实聊天记录，只放你自己的加密盘，不要上传、不要发人。**
