#!/usr/bin/env bash
# restore.sh —— 在新服务器上一键恢复小鲸鱼
#
# 用法（在新机器的 Ubuntu 上）：
#   bash restore.sh <数据包.tar.gz> [代码目录]
#
# 前置条件：
#   - 已 git clone 代码到 ~/whale-bot（或用第二个参数指定目录）
#   - 数据包里含 data/ 与 .env（由旧机 `tar czf ... data .env` 生成）
#   - 已装 python3-venv、docker
#
# 它做这些事：解数据 → 建 venv → 装依赖 → 装 systemd → 起服务 → 自测
set -euo pipefail

PKG="${1:?用法: bash restore.sh <数据包.tar.gz> [代码目录]}"
CODE="${2:-$HOME/whale-bot}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "1/7 检查代码目录"
[ -f "$CODE/bot.py" ] || { echo "找不到 $CODE/bot.py，请先 git clone 代码"; exit 1; }
cd "$CODE"

say "2/7 解开数据与 .env（覆盖同名文件）"
tar xzf "$PKG" -C "$CODE"
[ -f .env ] || { echo "数据包里没有 .env，请手动创建"; exit 1; }
chmod 600 .env
ls -la data | head -8

say "3/7 建虚拟环境并装依赖"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
if [ -f pip-freeze.txt ]; then
  .venv/bin/pip install -q -r pip-freeze.txt
else
  .venv/bin/pip install -q -r requirements.txt
fi

say "4/7 安装 systemd user service"
mkdir -p "$HOME/.config/systemd/user"
sed "s#%h#$HOME#g" deploy/whale-bot.service > "$HOME/.config/systemd/user/whale-bot.service"
systemctl --user daemon-reload
systemctl --user enable whale-bot

say "5/7 开 linger（否则 SSH 退出后被杀）"
sudo loginctl enable-linger "$USER" || echo "  （需要 sudo；失败请手动执行）"

say "6/7 起服务"
systemctl --user restart whale-bot
sleep 8
systemctl --user is-active whale-bot

say "7/7 自测"
.venv/bin/python selfcheck.py | tail -3

say "完成。接下来手动确认："
cat <<'EOF'
  1) docker 里的 OneBot 已登录：  sudo docker logs --tail 30 snowluma
  2) 上报口在本机可达：           curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/health
  3) QQ 里发 /指令 有回应
  4) 日志无 Traceback：           journalctl --user -u whale-bot -n 30
  5) 时区正确：                   timedatectl | grep 'Time zone'
EOF
