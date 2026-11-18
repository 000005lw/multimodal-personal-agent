# 可靠性升级交接说明

> 交接日期：2026-11-18
>
> 当前分支：`main@8d25d806464e27e30866edfc4d702edb73786b7a`
>
> 原始基线：`main@8010343a7f9a023f45192c037b7a3117a42ed200`

本文给下一位开发者说明本轮改了什么、怎么测试、上线时注意什么，以及哪些问题不要误解。

---

## 1. 快速验证

```bash
python -W error::SyntaxWarning -m compileall -q bot.py whale scripts tools

python scripts/selfcheck.py
python tools/action_replay.py --json
python tools/reliability_check.py --json
```

预期：

```text
selfcheck: 159/159
action replay: 36/36
reliability check: 15/15
```

所有测试都不访问真实 QQ、DeepSeek、家庭电脑或生产数据库。

GitHub Actions 最新验证：

```text
run 44293363848
head 8d25d806464e27e30866edfc4d702edb73786b7a
conclusion success
```

---

## 1.1 当前目录

```text
bot.py                  稳定入口，内部调用 whale.bot:main()
whale/agent/            动作、运行时、台账和 LLM
whale/memory/           对话摘要与神经元记忆
whale/relationship/     好感度、情绪与性格
whale/behavior/         规则与定时
whale/media/            语音、唱歌、图片和媒体
whale/integrations/     OneBot、家庭 worker、邮件和系统检查
scripts/                selfcheck、曲库、记忆和运维脚本
assets/                 人设与歌单
tools/                  家庭中继和离线回放
docs/                   技术文档
legal/MIT.txt           许可证正文
```

根目录不应再新增源码模块。业务模块放 `whale/`，一次性脚本放 `scripts/`，人工资源放 `assets/`。

## 2. 新增数据文件

运行后会创建：

```text
data/runtime.db   动作生命周期台账
data/jobs.db      家庭任务台账
```

它们和 `data/neuron.db` 一样是私密运行状态，不能提交仓库。

新增配置见 `.env.example`：

```env
RUNTIME_DB_FILE=runtime.db
JOB_DB_FILE=jobs.db
JOB_LEASE_SECONDS=1800
JOB_MAX_ATTEMPTS=3
JOB_RETENTION_DAYS=7
JOB_RESULT_MAX_BYTES=16777216
```

租约必须大于最长媒体任务。唱歌和视频都可能在 10 分钟以上，所以默认是 30 分钟，而不是几分钟。

---

## 3. 记忆迁移

### 旧 schema

```sql
text TEXT UNIQUE
```

### 新 schema

```sql
UNIQUE(cid, text)
```

迁移入口：

```python
neuron.connect(path)
```

迁移特点：

- 表重建在 `BEGIN IMMEDIATE` 中完成。
- 原 `id` 不动，保证 synapse 引用有效。
- 复制后校验节点数。
- 失败回滚。

### 上线前

必须先备份：

```bash
systemctl --user stop whale-bot
cp -a data/neuron.db data/neuron.db.bak-$(date +%F-%H%M)
```

然后启动一次服务或运行迁移测试。不要在服务运行中手改 `neuron.db`。

### 回滚

如果迁移失败：

1. 停止服务。
2. 恢复备份的 `neuron.db`。
3. 回到旧版本代码。
4. 查看日志里的 `neuron 迁移` 相关异常。

如果迁移成功但业务有问题，也应恢复完整 `data/`，不要只恢复单表。

---

## 4. 动作台账

文件：

```text
data/runtime.db
```

主要状态：

```text
in_flight
delivered
unconfirmed
failed_retryable
failed_terminal
denied
skipped
unregistered
unknown
```

### 重试

`ActionRuntime` 默认不自动重试失败动作。

需要显式：

```python
runtime = ActionRuntime(..., ledger=store, retry_failed=True)
```

### 超时

`TimeoutError/OSError` 会进入 `unknown`。不要把它直接改成自动重试，因为消息可能已经发出去，只是响应丢了。

### 启动恢复

`ActionStore.recover_inflight()` 会把上次进程里的 `in_flight` 改成 `unknown`。这是保守选择。

### Trace

- 自由文本参数不存原文。
- 只存参数 hash、长度或安全枚举。
- 不存聊天正文和真实 QQ 号。

---

## 5. 家庭任务台账

文件：

```text
data/jobs.db
```

状态：

```text
pending -> claimed -> result_ready -> delivering -> consumed
                    \-> failed / unknown
```

### 创建

`_ask_home()` 先写 `jobs.db`，再等待 Future。

### 领取

`GET /jobs/poll` 调用：

```python
store.claim_next(JOB_LEASE_SECONDS)
```

领取会原子更新：

- `status=claimed`
- `attempts += 1`
- `claimed_at`
- `lease_until`

### 结果

`POST /jobs/result`：

1. 先 `save_result()`。
2. 有原 Future 就投递给等待者。
3. 无 Future 且结果有音频，启动补发。
4. 重复结果返回 `{"dup": true}`。

### 恢复

`_recover_durable_jobs()`：

- 释放过期租约。
- 发现 `result_ready` 的音频结果并补发。
- 清理过期已结束任务。

### 不要误解

这不是 Exactly Once。

外部发送成功但确认丢失时，只能知道状态不确定。正确状态是 `unknown`，不是重新发送到确认成功。

---

## 6. 异步 Embedding

新增：

```python
await neuron.embed_many_async(texts)
await neuron.embed_async(text)
await neuron.activate_async(conn, text, cid=...)
```

同步版本仍保留：

```python
neuron.embed_many(texts)
neuron.embed(text)
neuron.activate(conn, text, cid=...)
```

这样做是为了：

- 老脚本继续可用。
- selfcheck 和迁移工具无需强制改成 async。
- 热路径使用异步 I/O。

注意：

- SQLite connection 仍由主事件循环线程持有。
- 不要把同一个 connection 丢给任意线程。
- 哈希向量降级仍然存在。
- 网络失败时可能退化为哈希向量，召回质量会下降。

---

## 7. 健康检查

```text
GET /health
```

返回示例：

```json
{
  "ok": true,
  "jobs": {
    "pending": 0,
    "claimed": 0,
    "result_ready": 0,
    "consumed": 3
  },
  "actions": {
    "delivered": 12,
    "unknown": 1
  }
}
```

这可用于快速判断是否有任务卡住或动作处于未知状态。

---

## 8. 建议上线顺序

1. 停服务并备份 `data/`。
2. 部署新代码。
3. 运行 `scripts/selfcheck.py`、`tools/action_replay.py`、`tools/reliability_check.py`。
4. 启动服务，观察 `neuron` 迁移日志。
5. 检查 `/health` 的 jobs/actions 统计。
6. 让主人在私聊中做一次 `/指令` 和普通聊天。
7. 让家庭电脑刷新代码并重启 relay。
8. 做一次短测试任务，确认 `jobs.db` 从 pending 到 consumed。

---

## 9. 当前已知边界

- `whale/bot.py` 没有做全量拆分。
- 动作和家庭任务使用两个 SQLite，不是统一事务。
- 家庭 worker 没有 heartbeat，只依赖长租约。
- 非音频结果在等待者丢失后保留在 `result_ready`，不会自动重建业务回调。
- 没有生产环境性能数据。
- 没有端到端 Exactly Once。
- Trace 脱敏已经加强，但新增工具仍需定义自己的安全参数策略。

---

## 10. 后续可选方向

如果未来继续维护，优先考虑：

1. worker heartbeat，缩短故障发现时间。
2. 动作台账与家庭任务台账的关联字段。
3. 非音频任务的恢复回调。
4. SQLite WAL/连接生命周期评估。
5. `whale/bot.py` 只做局部提取，不做一次性重构。
6. 在真实服务器上记录 P50/P95 和 RSS，再考虑性能结论。
