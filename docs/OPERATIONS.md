# 运维手册（OPERATIONS）

> 目标读者：在自己机器上把它跑起来、或者接手维护的人。
> 所有敏感值（IP / QQ / key）在仓库里都是占位符，请以你自己的 `.env` 为准。

---

## 1. 环境清单

| 组件 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.10+ | 用 venv，不要装到系统 Python |
| 依赖 | `pip install -r requirements.txt` | 必需只有 `aiohttp`；`httpx` 用于 embedding、`numpy` 用于向量 |
| QQ 桥接 | OneBot v11 后端（推荐 SnowLuma，docker） | 见 §3 |
| 服务器 | 1 核 2G 起 | 只跑 bot，不跑模型 |
| 剧本进程守护 | systemd user service | 见 §4 |
| （可选）家里电脑 | Windows + 显卡 + ffmpeg | 唱歌/看视频才需要 |

内存实测：bot 常驻约 **120–200 MB**（含 numpy/embedding 缓存），2G 机器够用但别再加服务。

---

## 2. 配置文件 `.env`

复制 `.env.example` 后填。**注意：值后面不能写行内注释**（本项目的解析器很朴素，注释会变成值的一部分）。

| 键 | 必填 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | ✅ | 对话模型 |
| `DEEPSEEK_BASE_URL` | | 默认 `https://api.deepseek.com` |
| `DASHSCOPE_API_KEY` | ✅ | 记忆向量（`text-embedding-v3`）；缺了会退化成哈希向量 |
| `ONEBOT_API_BASE` | ✅ | 发消息的地址，如 `http://127.0.0.1:3001` |
| `ONEBOT_ACCESS_TOKEN` | ✅ | 与 OneBot 后端一致 |
| `BOT_HOST` / `BOT_PORT` | ✅ | 上报监听，`0.0.0.0:8090`（在容器里访问宿主时要 0.0.0.0） |
| `OWNER_QQ` | ✅ | 主人 QQ（单个） |
| `FRIEND_QQ` | | 朋友白名单，逗号分隔 |
| `HOME_TOKEN` | | 家里电脑领活用的 token |
| `ACTIONS_V2` | | 轻量工具运行时开关，默认关闭 |
| `ACTION_TRACE_ENABLED` | | 是否记录脱敏动作 trace，默认开启 |
| `ACTION_TRACE_FILE` | | trace 文件名，默认 `action_trace.jsonl` |
| `RUNTIME_DB_FILE` | | 动作台账文件名，默认 `runtime.db` |
| `JOB_DB_FILE` | | 家庭任务台账文件名，默认 `jobs.db` |
| `JOB_LEASE_SECONDS` | | Worker 领取任务后的租约秒数，默认 1800 |
| `JOB_MAX_ATTEMPTS` | | 单任务最大领取次数，默认 3 |
| `JOB_RETENTION_DAYS` | | 已结束任务保留天数，默认 7 |
| `JOB_RESULT_MAX_BYTES` | | 单任务结果最大字节数，默认 16MB |
| `SMTP_*` | | 掉线邮件通知（可选） |
| `TOPIC_RESET_MIN` | | 隔多久换话题，默认 45 |
| `FRIEND_SILENT_STOP_DAYS` | | 朋友几天没回就静音，默认 5 |

---

## 3. QQ 桥接（OneBot v11）

```bash
docker run -d --name snowluma --restart unless-stopped \
  -p 127.0.0.1:3001:3001 \
  -e ONEBOT_ACCESS_TOKEN=<与 .env 一致> \
  motricseven7/snowluma:latest
```

首次登录需要扫码，两种方式：

1. 容器日志里会打印二维码路径/链接（`docker logs -f snowluma`）；
2. 本项目自带邮件通道：给配置的邮箱发一封**主题含「扫码」**的邮件，bot 会把二维码发回来。

**只绑 127.0.0.1**：上报地址由容器主动 POST 到宿主，不需要对外暴露 3001。

验证连通：

```bash
curl -s http://127.0.0.1:3001/get_status -H "Authorization: Bearer <token>"
# {"status":"online"...}
```

---

## 4. 部署（Linux + systemd user service）

```bash
# 1) 代码
git clone <repo> ~/whale-bot && cd ~/whale-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2) 配置
cp .env.example .env && vim .env        # 填 key / QQ / token

# 3) 服务单元（模板见 deploy/whale-bot.service）
mkdir -p ~/.config/systemd/user
cp deploy/whale-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now whale-bot
loginctl enable-linger $USER            # 关键：否则退出 SSH 就被杀

# 4) 看日志
journalctl --user -u whale-bot -f
```

**systemd 必须带 `Restart=always` 和 `Environment=PYTHONUNBUFFERED=1`**，
否则崩溃后不重启、日志还会被缓冲吞掉。

### 更新流程（改完代码之后）

```bash
# 本地
python scripts/selfcheck.py   # 必须 159/159
# 服务器
git pull && systemctl --user restart whale-bot
journalctl --user -u whale-bot -n 30      # 确认没有 Traceback
```

---

## 5. 家里电脑（可选，唱歌/看视频用）

```powershell
# 1) 依赖（只在需要唱歌时）
pip install demucs torch soundfile librosa        # 大件，按显卡装
# 2) 音频工具
winget install ffmpeg                              # 或自行下载放进 PATH
# 3) 起中继（它会自己建隧道、循环领活）
python tools\whale-relay.py
```

要点：

- 中继启动时会 `ssh -N -L 8091:127.0.0.1:8090 <server>`，
  **要求该机器的 SSH 公钥已加到服务器**（免密）；
- 改了 `whale/media/sing.py` / `whale/integrations/home_agent.py` **必须重启中继窗口**，
  否则版本握手会提示“旧版程序”；
- 断线不用管：中继会重连、结果落盘、下次启动补交。

---

## 6. 排障手册

| 症状 | 先查什么 | 常见原因 |
| --- | --- | --- |
| 完全不回消息 | `journalctl --user -u whale-bot -n 50`；`curl :3001/get_status` | 桥接掉线 / 上报地址错 / 白名单没加 |
| 回得很慢 | 日志里的 `HTTP Request` 耗时 | DeepSeek 限流；或 `build_messages` 塞了太多事实 |
| 说“家里那台电脑没连着” | 服务器日志 `[家里电脑]`；中继窗口是否开着 | 中继没开 / 版本不匹配 |
| 唱歌发的是游戏解说 | 日志里 `选中：` 那一行 | 曲库里有脏条目（旧版本存进去的）；删掉对应 key |
| 声音变调很怪 | 日志 `人声主频 … 变调 ±N 半音` | 往下压超过 2 个半音（已限制）；缓存里有旧音频 |
| 她老重复同一句 | `/查好感` 看是否门禁刷屏 | 门禁文案的 6 小时冷却失效 |
| 定时没响 | `journalctl | grep 定时` | 排到了明天（看日志里的完整日期） |
| 记忆检索退化 | `data/shadow_daily.jsonl` 最后几行 | embedding key 失效 → 退化成哈希向量 |
| 容器里连不上宿主 | `.env` 里 `BOT_HOST` | 应该是 `0.0.0.0` 而不是 `127.0.0.1` |

### 常用诊断命令

```bash
# 谁在说话、她回了什么（带 cid，便于分辨是谁）
journalctl --user -u whale-bot --since '10 min ago' | grep -E '\[u[0-9]+\]'

# 记忆召回质量
tail -5 data/shadow_daily.jsonl

# 曲库有多少首、有没有脏条目
python -c "from whale.media import songlib; print(songlib.count()); print(songlib.listing()[:3])"

# 自测（离线、不花钱）
python scripts/selfcheck.py

# 36 条合成 Agent 动作回放
python tools/action_replay.py

# 记忆隔离、动作失败重试、任务租约和异步 Embedding
python tools/reliability_check.py
```

---

## 7. 备份与恢复

**备份**：`data/` 就是全部状态，直接打包即可（建议每日 + 加密）。

```bash
tar czf whale-data-$(date +%F).tar.gz data/
```

**恢复**：停服务 → 覆盖 `data/` → 起服务。

注意 `data/neuron.db` 是 sqlite，**先停服务再拷**，否则可能拷到写了一半的库。
`data/runtime.db` 和 `data/jobs.db` 同样是 SQLite，也必须停服务后一起备份。

---

## 8. 成本控制

- 对话模型：DeepSeek，按 token 计费。**记忆抽取是节流的**（同一个人攒 4 句 + 间隔 10 分钟才抽一次），
  这条节流是成本命门，别关。
- embedding：只在写入记忆、检索记忆时调用，且有磁盘缓存。
- 每日一次影子评测（20 样本）+ 一次记忆维护（无模型调用），成本可忽略。
- 想进一步省钱：把 `PROACTIVE_MIN/MAX_MINUTES` 调大（少主动）、关掉 `FRIEND_PROACTIVE_ENABLED`。

---

## 9. 上线检查单

- [ ] `.env` 齐全，且**没有**把 `.env` 提交进仓库
- [ ] `python scripts/selfcheck.py` → 159/159
- [ ] OneBot `get_status` 为 online
- [ ] 主人在 QQ 里发 `/指令` 有回应
- [ ] 白名单外的号发消息**无任何回应**
- [ ] 朋友号发消息，回复里**不出现“爸爸”**
- [ ] `journalctl` 里没有 Traceback
- [ ] `loginctl enable-linger` 已开（防 SSH 退出被杀）
