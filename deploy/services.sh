#!/usr/bin/env bash
# NovelKit 面板的临时启动脚本 —— 在装好 systemd 之前的过渡手段（也可用于云服务器快速试跑）。
#
#   ./deploy/services.sh start      # 启动（已在跑则先停）
#   ./deploy/services.sh stop       # 停止
#   ./deploy/services.sh restart
#   ./deploy/services.sh status
#
# 监听地址/端口/访问令牌读 webpanel/panel.env（没有就用默认 127.0.0.1:8897）。
# 与直接在终端里 `python server.py &` 的区别：这里用 setsid 把进程放进**新的会话**
# 并重定向输出，父 shell 退出（终端关闭、agent 任务回收）都不会把它带走。
# 但它没有开机自启、崩溃不会自动重启 —— 那些要 systemd，见 deploy/systemd/README.md。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/log"
PANEL_ENV="$ROOT/webpanel/panel.env"
PANEL_PID="$LOG_DIR/panel.pid"

# 默认值：只监听本机
PANEL_HOST=127.0.0.1
PANEL_PORT=8897
PANEL_TOKEN=""
# shellcheck disable=SC1090
[ -f "$PANEL_ENV" ] && set -a && . "$PANEL_ENV" && set +a
PANEL_HOST="${PANEL_HOST:-127.0.0.1}"
PANEL_PORT="${PANEL_PORT:-8897}"
PANEL_TOKEN="${PANEL_TOKEN:-}"

mkdir -p "$LOG_DIR"

# 端口占用的可靠判断：**连接探测**。本机读不到别的进程的 /proc/<pid>/fd
# （Permission denied），`ss -ltnp` 也不显示 PID，所以"拿 PID 去 kill"这条路不一定通；
# 但"有没有人在监听"永远测得准。探测逻辑据此写成：能连上就是被占用。
port_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- && return 0
  return 1
}

# 找出占用端口的 PID：优先 /proc 反查（精确），其次 ss -p（root 下可用）。
# 某些加固内核会隐藏 /proc/<pid>/fd 与 ss 的 PID 列（连自己的进程也读不到），
# 于是最后按"是谁在跑我们的面板"兜底——这里只有一个受管端口，收口是安全的。
port_pids() {
  local port="$1" pids=""
  pids="$(python3 "$ROOT/deploy/port_pids.py" "$port" 2>/dev/null | tr '\n' ' ' || true)"
  if [ -z "${pids// /}" ]; then
    pids="$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $NF}' \
            | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u | tr '\n' ' ' || true)"
  fi
  if [ -z "${pids// /}" ]; then
    pids="$(pgrep -f 'webpanel/server\.py' 2>/dev/null | tr '\n' ' ' || true)"
  fi
  printf '%s' "$pids"
}

# 结束占用端口的进程；**结束时再探测一次**，仍被占用就报错退出。
free_port() {
  local port="$1" pids
  if ! port_open "$port"; then
    return 0
  fi
  pids="$(port_pids "$port")"
  if [ -n "${pids// /}" ]; then
    echo "      端口 $port 被占用，结束进程: $pids"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
  if port_open "$port"; then
    echo "[错误] 端口 $port 仍被占用，但拿不到占用进程的 PID（本机限制读 /proc）。" >&2
    echo "       请先手动停掉它，例如： sudo fuser -k -n tcp $port" >&2
    return 1
  fi
  return 0
}

stop_panel() {
  local pid
  pid="$(cat "$PANEL_PID" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
    echo "  已停止面板 (pid $pid)"
  fi
  rm -f "$PANEL_PID"
  free_port "$PANEL_PORT" || true
}

start_panel() {
  cd "$ROOT"
  PY=".venv/bin/python"
  [ -x "$PY" ] || PY="python3"
  PANEL_HOST="$PANEL_HOST" PANEL_PORT="$PANEL_PORT" PANEL_TOKEN="$PANEL_TOKEN" \
    setsid nohup "$PY" webpanel/server.py >>"$LOG_DIR/panel.log" 2>&1 < /dev/null &
  echo $! > "$PANEL_PID"
  local shown="$PANEL_HOST"
  [ "$shown" = "0.0.0.0" ] && shown="<服务器 IP>"

  # 等它真的起来：起不来就打印日志尾部，绝不只报一句"已启动"（否则端口被占时
  # 新进程会 bind 失败秒退，而终端看起来一切正常）。
  local i code=000
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.3
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
      "http://127.0.0.1:$PANEL_PORT/api/health" 2>/dev/null || echo 000)"
    [ "$code" = "200" ] && break
    kill -0 "$(cat "$PANEL_PID")" 2>/dev/null || break
  done
  if [ "$code" != "200" ]; then
    echo "[错误] 面板没有起来（/api/health HTTP $code）。日志尾部：" >&2
    tail -n 15 "$LOG_DIR/panel.log" >&2 || true
    return 1
  fi

  echo "  面板已启动 (pid $(cat "$PANEL_PID")) → http://$shown:$PANEL_PORT/"
  [ -n "$PANEL_TOKEN" ] && echo "  已启用访问令牌：首次打开用 http://$shown:$PANEL_PORT/?token=<令牌>"
}

status() {
  local pid
  pid="$(cat "$PANEL_PID" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    local probe
    probe="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
      "http://127.0.0.1:$PANEL_PORT/" 2>/dev/null || echo 000)"
    echo "  面板: 运行中 (pid $pid) · HTTP $probe · 端口 $PANEL_PORT"
  elif port_open "$PANEL_PORT"; then
    echo "  面板: 端口 $PANEL_PORT 被其它进程占用（非本脚本启动）"
  else
    echo "  面板: 未运行"
  fi
}

case "${1:-status}" in
  start)
    echo "启动服务："
    stop_panel
    start_panel
    sleep 2
    echo
    status
    ;;
  stop)
    echo "停止服务："
    stop_panel
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status) status ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
