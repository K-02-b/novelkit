#!/usr/bin/env bash
# 启动 Web 面板。
#
#   ./webpanel/run.sh                        # 本机：http://127.0.0.1:8897/
#   ./webpanel/run.sh --port 9000            # 换端口
#   ./webpanel/run.sh --open                 # 启动后自动打开浏览器
#   ./webpanel/run.sh --host 0.0.0.0 --token <令牌>   # 云服务器（见 deploy/cloud.md）
#
# 也可以把 PANEL_HOST / PANEL_PORT / PANEL_TOKEN 写进 webpanel/panel.env，
# 不带参数直接运行即可（systemd 单元读的也是这个文件）。
# 面板本身零第三方依赖（纯标准库）；用 .venv 是为了让面板触发的翻译任务能 import openai。
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="webpanel/panel.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"
# 不缓冲 stdout：被重定向到文件/nohup 时也能立刻看到启动横幅（监听地址与端口）。
exec env PYTHONUNBUFFERED=1 "$PY" webpanel/server.py "$@"
