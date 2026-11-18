# 2026-11-18 可靠性升级报告

> 基线：`main@8010343a7f9a023f45192c037b7a3117a42ed200`
>
> 当前合并提交：`main@8d25d806464e27e30866edfc4d702edb73786b7a`
>
> 可靠性分支：`codex/final-agent-reliability`
>
> 目的：在保持单进程 Python、2GB 服务器、aiohttp、SQLite 和家庭电脑中继架构不变的前提下，补齐 Agent 动作生命周期、数据隔离、任务恢复和异步检索可靠性。

---

## 1. 先说明边界

这不是一个从零开发的项目。

原项目已经具备：

- OneBot v11 QQ 接入
- DeepSeek 对话
- 人格与关系状态
- 短期记忆 + 神经元长期记忆
- 好感度、情绪、规则、定时
- 语音、唱歌、视频、图片、搜索
- SSH 隧道和家庭 Windows worker
- 离线 selfcheck

本轮工作是在既有架构上做二次开发，重点不是增加人格玩法或更多模型，而是修复实际可靠性问题，让工具调用和异步任务的“执行结果”可解释、可测试。

---

## 2. 接手时发现的主要问题

### 2.1 不同用户的相同记忆会被错误合并

`neuron` 表早期使用：

```sql
text TEXT NOT NULL UNIQUE
```

而 `add_node()` 只按 `text` 查询：

```sql
SELECT id FROM neuron WHERE text = ?
```

结果：

```text
用户 A 写入“喜欢喝茶” -> ID 1
用户 B 写入“喜欢喝茶” -> 仍然返回 ID 1
```

检索虽然没有跨 cid 返回节点，但写入隔离已经错误。用户 B 没有自己的同文本记忆，A 的访问统计还可能被错误增加。

### 2.2 动作失败后不能被安全重试

原 `ActionRuntime._record_once()` 在真正执行 Handler 之前就把 action ID 放进已处理集合。

结果是：

```text
第一次执行：failed
第二次同 ID：skipped duplicate_action_id
```

这能防止某些重复副作用，但会让“明确可重试的失败”永久失去重试机会。

### 2.3 `success`、`skipped`、`delivered` 语义混在一起

旧逻辑中：

- Handler 返回 `success` 不一定代表 QQ 已经收到。
- `skipped` 可能是正常跳过，也可能没有任何交付。
- `reply()` 只要前置动作 `success` 或 `skipped` 就可能提前结束，普通回复也可能被吞掉。

### 2.4 家庭任务状态只在内存

旧任务链路：

```text
asyncio.Queue JOBS
PENDING Future
app["delivered"] set
```

服务重启时：

- 尚未领取的任务会消失。
- 已领取任务失去原来的等待者。
- 结果回传后可能只能靠音频补发兜底。
- 重复提交只能靠进程内 set 去重。

### 2.5 Embedding 网络请求可能阻塞事件循环

`neuron.embed_many()` 使用同步 `httpx.post()`，最坏等待时间可达 40 秒。
`build_messages()` 又在回复前同步调用 `_neuron_recall()`。

这会让一个慢请求拖住单进程内其他会话。

### 2.6 Trace 仍可能记录自由文本参数

旧 Trace 记录 `arg[:80]`。虽然不记录聊天原文和真实 QQ，但搜索词、歌名、查询和 URL 仍可能带隐私信息。

### 2.7 relay token 仍兼容 URL query

中继实际已经使用 `X-Home-Token`，但服务端 `_check_home_token()` 仍接受 query token。为了减少代理和访问日志泄露面，本轮移除了该兼容入口。

### 2.8 仓库根目录混放源码、脚本、人设和历史文件

接手时根目录同时放着 20 多个 Python 模块、运维脚本、人设、歌单、许可证和重复交接文档。这种平铺结构会出现几个问题：

- 核心模块、一次性脚本和运行入口混在一起， GitHub 首页无法快速看出项目结构。
- 脚本移动后很容易遗漏 import 或相对路径。
- GitHub 会因为根目录存在 `CONTRIBUTING.md`、`SECURITY.md`、`LICENSE` 自动生成额外标签页。
- `FOR-CONTRIBUTOR.md` 与 `CONTRIBUTING.md` 内容重复，长期会出现两份文档不一致。

当前已经重组为 `whale/` Python 包、`scripts/` 脚本目录、`assets/` 资源目录、`legal/` 许可证目录，并删除重复的贡献者文件。

### 2.9 CI 情绪测试存在随机失败

`emotion.consistent()` 对明确情绪仍保留 25% 的随机分支。测试连续采样 30 次并要求至少 20 次返回“开心”，在 Linux CI 上实际出现过 18/30，导致 selfcheck 偶发失败。

现在明确情绪会确定性返回对应分类，不再被随机概率覆盖。

---

## 3. 本轮完成的四组改造

## 3.1 PR-A：跨用户记忆隔离

### 改动

- `neuron` 唯一约束改为 `UNIQUE(cid, text)`。
- `add_node()` 改为按 `(cid, text)` 查找。
- 增加旧数据库事务式迁移。
- 迁移保留节点 ID，确保 `synapse.a/b` 引用不断。
- `update_node()` 在文本更新触发唯一冲突时返回失败，不再让异常穿透业务。
- 增加旧 schema、节点 ID、突触和跨 cid 检索测试。

### 迁移方式

SQLite 不能原地修改列级 UNIQUE 约束，因此使用表重建：

```text
BEGIN IMMEDIATE
  create neuron_migrated with UNIQUE(cid, text)
  copy all rows including original ids
  verify row count
  drop old neuron
  rename migrated table
  recreate indexes
COMMIT
```

失败会 `ROLLBACK`，旧表仍可继续使用。

### 验收

- 不同用户可保存完全相同的记忆。
- 检索结果不会跨 cid 混用。
- 旧节点 ID 和突触保留。
- 迁移节点数不一致时拒绝提交。

---

## 3.2 PR-B：Agent 动作生命周期

### 新增 `whale/agent/action_store.py`

SQLite 表：

```sql
action_runtime (
  action_id, cid, kind, arg_hash, arg_length, source,
  status, delivered, retryable, attempts,
  created_at, updated_at, started_at, finished_at, error_code
)
```

### 状态语义

```text
in_flight
  -> delivered
  -> unconfirmed
  -> failed_retryable
  -> failed_terminal
  -> denied
  -> skipped
  -> unregistered
  -> unknown
```

### 行为变化

1. 动作先进入 `in_flight`。
2. 并发重复提交会得到 `duplicate_in_flight`。
3. 已交付动作再次提交会得到 `duplicate_delivered`。
4. `failed_retryable` 只有显式 `retry_failed=True` 才能重试。
5. `TimeoutError/OSError` 标记 `unknown`，不自动重放。
6. `success` 与 `delivered` 分开。
7. `skipped`、`unregistered`、`unconfirmed` 不再让前置动作提前吞掉正常回复。
8. 服务启动时将上次 `in_flight` 动作标记为 `unknown`，避免盲目重放。

### 结果示例

```json
{
  "status": "success",
  "delivery_status": "delivered",
  "delivered": true,
  "retryable": false,
  "attempt": 1
}
```

### Trace 脱敏

- 自由文本参数只记录 SHA256 前 24 位和长度。
- 枚举型安全参数（情绪、introspect 主题）可以保留。
- 不再默认记录搜索词、歌名、URL 等自由文本。

---

## 3.3 PR-C：家庭任务持久化与恢复

### 新增 `whale/agent/job_store.py`

表：

```sql
jobs (
  id, kind, conversation_id, payload_json, payload_hash,
  status, attempts, max_attempts, created_at, updated_at,
  claimed_at, lease_until, finished_at,
  result_json, result_hash, delivered_at, error_code
)
```

### 状态

```text
pending
  -> claimed
  -> result_ready
  -> delivering
  -> consumed

pending/claimed
  -> failed
  -> unknown
```

### 关键变化

1. `_ask_home()` 创建任务时先写 `data/jobs.db`。
2. `/jobs/poll` 从 SQLite 原子领取任务。
3. 领取时写入 `lease_until`，避免多个 worker 同时拿同一任务。
4. 默认租约 1800 秒，大于唱歌和视频处理最长耗时。
5. `/jobs/result` 先持久化结果，再通知原 Future。
6. 重复结果返回 `dup: true`，不覆盖已完成结果。
7. 服务重启时释放过期租约。
8. `result_ready` 的音频结果可由无等待者恢复流程补发。
9. ASR token 不写入任务表，只在 worker 领取时临时注入。
10. `/health` 增加 jobs/actions 状态统计。

### 仍需明确的边界

这是“任务可恢复、状态可追踪、尽量避免重复副作用”，不是端到端 Exactly Once。

如果 QQ 已经收到消息但 HTTP 响应丢失，系统仍无法仅靠本地数据库证明对方一定收到了。对于这种状态必须使用 `unknown`，不能自动假定失败并重复发送。

---

## 3.4 PR-D：异步 Embedding

### 改动

- 新增 `neuron.embed_many_async()`。
- 新增 `neuron.embed_async()`。
- 新增 `neuron.activate_async()`。
- aiohttp 异步请求 Embedding。
- `build_messages()` 支持传入预先取得的 recall 结果。
- `reply()`、表情包回复、定时提醒、视频总结使用异步 recall。
- `_neuron_learn()` 先异步批量生成向量，再写节点，避免在事件循环里同步请求。
- 保留原同步 `embed_many()`，兼容脚本、迁移和测试。
- 增加向量维度不一致时的安全余弦保护。

### 效果

慢 Embedding 请求不再直接阻塞单进程 aiohttp 事件循环中的其他任务。

### 未宣称

没有真实生产 Embedding 延迟数据，因此不能宣称“P95 降低多少”。当前只能确认代码路径已经不再使用同步网络请求。

## 3.5 仓库结构与 CI 修复

### 目录重组

当前主结构：

```text
bot.py                  稳定启动包装器
whale/agent/            动作协议、运行时、台账、LLM
whale/memory/           短期和长期记忆
whale/relationship/     好感度、情绪、表情和性格快照
whale/behavior/         规则与提醒
whale/media/            语音、唱歌、图片和媒体
whale/integrations/     OneBot、家庭 worker、邮件和系统状态
scripts/                自测、曲库、记忆工具和运维脚本
assets/                 人设和歌单
legal/MIT.txt           MIT 许可证正文
docs/                   架构、运维、安全和升级报告
tools/                  家庭中继和离线回放
```

### GitHub 首页

原先 GitHub 自动显示 README、Contributing、MIT license、Security 四个标签。
这是 GitHub 对根目录 `CONTRIBUTING.md`、`LICENSE`、`SECURITY.md` 的自动识别。

现在这些文件被迁移和改名：

- `docs/DEVELOPMENT.md`
- `docs/SECURITY-MODEL.md`
- `legal/MIT.txt`

GitHub 首页已确认只显示 README。

### README

README 已从功能罗列改成完整项目说明，包含：

- 项目定位
- 系统结构
- 仓库目录
- 快速开始
- 配置
- Agent 工具运行时
- 家庭任务
- 开发测试
- 数据隐私
- 部署
- 文档索引
- 当前限制

### CI

Workflow 现在直接编译整个 `bot.py + whale/ + scripts/ + tools/`，并运行：

```text
python scripts/selfcheck.py
python tools/action_replay.py
python tools/reliability_check.py
```

最新 GitHub Actions 运行已通过。

---

## 4. 参考了哪些设计

本轮没有复制其他 GitHub 项目的源码。

采用的是一组通用工程模式：

### 4.1 SQLite 表重建迁移

参考 SQLite 对 schema 变更的通用做法：

- 新表
- 事务内复制数据
- 校验
- 删旧表
- 重命名
- 重建索引

### 4.2 Lease-based worker queue

家庭任务领取使用：

- `claimed_at`
- `lease_until`
- `attempts`
- `max_attempts`

这是任务队列和 worker 系统中常见的租约模式。

### 4.3 Transactional Outbox 思路

动作和任务先把意图写入 SQLite，再执行外部副作用。这个思路与 Outbox 相似，但当前项目没有完整的跨系统事务，也没有实现端到端 Exactly Once。

### 4.4 显式状态机

把 `success`、`delivered`、`unknown`、`retryable` 分开，而不是用单一布尔值表示所有结果。

### 4.5 异步 I/O 隔离

使用 aiohttp 异步客户端处理 Embedding，避免同步 `httpx.post()` 阻塞事件循环。

---

## 5. 没有采用的东西

本轮没有引入：

- LangGraph
- MCP 服务框架
- Redis
- Celery
- 新的向量数据库
- 多进程 worker
- Web 管理后台

原因是当前系统是低并发、单用户、2GB 服务器场景，新增基础设施的成本会超过收益。

---

## 6. 测试与验收

### 已有测试

```text
python scripts/selfcheck.py
159/159

python tools/action_replay.py --json
36/36
```

### 新增可靠性检查

```text
python tools/reliability_check.py
15/15
```

覆盖：

- 旧 neuron schema 迁移
- 节点 ID 和突触保留
- 不同 cid 保存相同文本
- 不同 cid 检索不混用
- 可重试失败动作
- 显式重试
- 跨运行时重启的已交付去重
- 超时标记 unknown
- Trace 自由文本脱敏
- 任务持久化和 worker 领取
- 过期租约恢复
- 重复结果拒绝
- 独占交付状态
- 异步 Embedding 不阻塞其他协程
- relay 不再接受 URL query token

### CI

`.github/workflows/selfcheck.yml` 已加入：

- 新模块编译
- selfcheck
- action replay
- reliability check

对应 GitHub Actions：

```text
run 44293363848
head 8d25d806464e27e30866edfc4d702edb73786b7a
conclusion success
```

---

## 7. 当前仍然存在的限制

### 7.1 `whale/bot.py` 仍然是巨石

本轮没有做全量重构。`reply()` 仍包含大量业务顺序和特殊路径。

### 7.2 家庭任务恢复还不是完整分布式系统

- 没有 worker heartbeat。
- 租约通过较长默认时间避免重复领取。
- 非音频结果在等待者消失后会保留为 `result_ready`，但不会自动重建原业务回调。
- 没有跨服务 Exactly Once。

### 7.3 动作台账和任务台账是两套 SQLite

- `runtime.db` 管动作生命周期。
- `jobs.db` 管家庭任务。

两者目前没有统一事务，因此不能声称一个动作与其所有外部任务原子提交。

### 7.4 真实生产数据未用于测试

所有测试使用临时数据库和合成数据，没有上传真实聊天、QQ 号、音频或生产数据库。

---

## 8. 可写进简历的内容

以下内容已经随 `main@8d25d80` 合并，并有本地与 GitHub Actions 测试记录支撑。

### 版本一，偏工具调用

> 参与开源多模态 QQ 个人 Agent 的可靠性优化，将模型动作解析与执行解耦，设计轻量 ActionRuntime 和 SQLite 动作台账，实现权限校验、执行/交付状态分离、失败重试、并发去重、未知结果保护和脱敏 Trace，并补充故障注入与离线回放测试。

### 版本二，偏异步任务

> 针对云端与家庭电脑协同处理媒体任务的状态丢失问题，基于 SQLite 设计轻量任务台账和租约机制，持久化任务创建、领取、结果与消费状态，支持进程重启后的过期任务恢复和重复结果去重。

### 版本三，偏数据与性能

> 修复跨会话长时记忆的 SQLite 唯一约束缺陷，设计兼容旧库的事务式迁移；将回复热路径的同步 Embedding 网络请求改为异步，并保留哈希向量降级。

### 不应写

- 从零独立开发整个 Agent。
- 实现端到端 Exactly Once。
- 生产环境工具成功率 100%。
- 已完成分布式任务系统。
- 独立设计原作者的全部记忆、语音、视频和人格功能。

---

## 9. 面试时应该能回答的问题

1. 为什么不能把 `text` 设为全局唯一？
2. SQLite 为什么需要表重建，而不是直接改 UNIQUE？
3. 为什么迁移必须保留原节点 ID？
4. 为什么动作失败后不能直接自动重试？
5. 为什么 `success` 和 `delivered` 要分开？
6. 动作在发送后、响应前崩溃，应该标记什么状态？
7. 为什么当前选择 SQLite，而不是 Redis 或 Celery？
8. 家庭任务如何避免被两个 worker 同时领取？
9. 为什么需要租约，租约时间过短会怎样？
10. 为什么不能宣称 Exactly Once？
11. 为什么 SQLite 连接不能随便传到其他线程？
12. 为什么保留同步 Embedding API？

---

## 10. 本轮主要文件

| 文件 | 说明 |
| --- | --- |
| `whale/memory/neuron.py` | 复合唯一约束、迁移、异步 Embedding、异步激活 |
| `whale/agent/action_store.py` | SQLite 动作台账 |
| `whale/agent/action_runtime.py` | 动作生命周期、交付语义、脱敏 Trace |
| `whale/agent/job_store.py` | 家庭任务台账、租约、结果和消费状态 |
| `whale/bot.py` | 接入动作台账、任务台账、异步 recall、健康统计 |
| `whale/config.py` | 新增 runtime/job 配置 |
| `tools/action_replay.py` | 适配新动作状态 |
| `tools/reliability_check.py` | 故障与迁移检查 |
| `.github/workflows/selfcheck.yml` | 编译和可靠性 CI |
| `docs/RELIABILITY-HANDOVER.md` | 给下一位开发者的交接说明 |
