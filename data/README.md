# data/ 目录说明

**这个目录不进仓库**（`.gitignore` 已排除）。运行时自动创建。

| 文件/目录 | 内容 |
| --- | --- |
| `memory.json` | 对话窗口 + 摘要 + 归档 |
| `neuron.db` | 记忆网络（sqlite，`neuron` / `synapse` 两张表） |
| `emb_cache.json` | embedding 缓存 |
| `affection.json` | 好感度 |
| `mood.json` | 瞬时情绪 |
| `rules.json` | 规则（作息/话题/定时/禁表情包） |
| `reminders.json` | 定时队列 |
| `contacts.json` | 称呼、关系、性别、勿扰、最后说话时间 |
| `state.json` | 跨重启状态（看视频记录、行动台账、最后那首歌） |
| `songs/index.json` + `songs/*.mp3` | 曲库 |
| `singing/` | 待发送的音频临时文件 |
| `announcements.jsonl` | 公告历史 |
| `shadow_daily.jsonl` | 每日记忆评测 |

## 备份

`data/` 就是全部状态。备份 = 打包这个目录（**先停服务再拷 sqlite**）。
