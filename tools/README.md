# tools/ —— 家里电脑用的脚本（可选）

唱歌、看 B 站视频需要一台常开的家用电脑跑这些脚本。不跑也能用：
机器人会提示「家里那台电脑没连着」，聊天/记忆/好感/规则功能全部正常。

| 文件 | 作用 |
| --- | --- |
| `whale-relay.py` | **入口**：维持 SSH 隧道 + 循环领活 + 交活重试 + 结果落盘补交 |
| `看记忆云.bat` | 拉取服务器上的 `neuron.db` → 生成 3D 记忆云 → 打开浏览器 |
| `编辑记忆云.bat` | 同上 + 起编辑服务，Ctrl+C 后把改动作为补丁上传 |

## 用法

```powershell
# 1) 先把本机 SSH 公钥加到服务器（免密）
ssh-copy-id <user>@<your-server>

# 2) 改 whale-relay.py 顶部的 SERVER / WORKSPACE 两行
# 3) 起中继（保持窗口开着）
python tools\whale-relay.py
```

改了 `sing.py` / `home_agent.py` 之后**必须重启这个窗口**，
否则服务器会回「家里那个黑窗口还开着旧版程序」（版本握手）。
