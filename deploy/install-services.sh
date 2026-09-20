#!/usr/bin/env bash
# 安装 NovelKit 面板的 systemd 单元（本机或云服务器通用）。
#
#   cd /path/to/novelkit && sudo ./deploy/install-services.sh
#   sudo /path/to/novelkit/deploy/install-services.sh     # 绝对路径，任意目录都行
#   ./deploy/install-services.sh --user                   # 用户级（需要 systemd 用户会话）
#   ./deploy/install-services.sh --uninstall              # 卸载（可配 --user）
#
# 安装前请先复制并编辑 webpanel/panel.env（云端部署需要 PANEL_TOKEN）：
#   cp webpanel/panel.env.example webpanel/panel.env
#
# 脚本做的事：迁移/停掉旧单元 → 释放端口 → 由模板生成单元 → daemon-reload
#             → enable --now → 报状态。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$PROJECT_DIR/deploy/systemd"
PANEL_UNIT="novelkit-panel.service"
TARGET_UNIT="novelkit.target"

MODE="system"
ACTION="install"
for arg in "$@"; do
  case "$arg" in
    --user) MODE="user" ;;
    --uninstall) ACTION="uninstall" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

# 早期版本用过这些单元名（产品改名 NovelKit 之前），以及曾由本仓库代管的外部工具单元；
# 单元名必须保留旧值，否则老用户的残留单元清不掉（下面只是注释，不影响功能）。
LEGACY_UNITS=("novel.target" "novel-panel.service" "tomato-downloader.service")

if [ "$MODE" = "user" ]; then
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  SYSTEMCTL=(systemctl --user)
  RUN_USER="$(id -un)"
  RUN_GROUP="$(id -gn)"
else
  UNIT_DIR="/etc/systemd/system"
  SYSTEMCTL=(systemctl)
  if [ "$(id -u)" = "0" ]; then
    # 以项目目录所有者的身份运行面板，避免译文文件属主变成 root
    RUN_USER="$(stat -c '%U' "$PROJECT_DIR" 2>/dev/null || echo root)"
    RUN_GROUP="$(stat -c '%G' "$PROJECT_DIR" 2>/dev/null || echo root)"
  else
    RUN_USER="$(id -un)"
    RUN_GROUP="$(id -gn)"
  fi
fi

if [ "$MODE" = "system" ] && [ "$(id -u)" != "0" ]; then
  echo "[错误] 系统级安装需要 root。请用 sudo 运行，或改用 --user。" >&2
  exit 1
fi

# 用户级安装前先确认用户 systemd 会话可用，否则会装成功但起不来
if [ "$MODE" = "user" ] && ! "${SYSTEMCTL[@]}" status >/dev/null 2>&1; then
  echo "[错误] 连不上用户级 systemd（No medium found / Failed to connect to bus）。" >&2
  echo "       需要真实登录会话，或让 root 执行： loginctl enable-linger $(id -un)" >&2
  exit 1
fi

if [ "$ACTION" = "uninstall" ]; then
  echo "[1/2] 停止并禁用 $TARGET_UNIT"
  "${SYSTEMCTL[@]}" disable --now "$TARGET_UNIT" 2>/dev/null || true
  echo "[2/2] 删除单元文件"
  rm -f "$UNIT_DIR/$PANEL_UNIT" "$UNIT_DIR/$TARGET_UNIT"
  for legacy in "${LEGACY_UNITS[@]}"; do rm -f "$UNIT_DIR/$legacy"; done
  "${SYSTEMCTL[@]}" daemon-reload
  echo "已卸载。"
  exit 0
fi

# 选解释器：优先项目自带的 .venv（已装 requests/beautifulsoup4）
PYTHON="$PROJECT_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

# 端口：优先读 webpanel/panel.env，否则默认 8897
PANEL_PORT=8897
PANEL_ENV="$PROJECT_DIR/webpanel/panel.env"
if [ -f "$PANEL_ENV" ]; then
  PORT_LINE="$(grep -E '^[[:space:]]*PANEL_PORT[[:space:]]*=' "$PANEL_ENV" | tail -n1 || true)"
  if [ -n "$PORT_LINE" ]; then
    PORT_LINE="${PORT_LINE#*=}"; PORT_LINE="${PORT_LINE//[[:space:]\"]/}"
    [ -n "$PORT_LINE" ] && PANEL_PORT="$PORT_LINE"
  fi
fi

# 端口占用的可靠判断：**连接探测**。拿不到别的进程 PID 时也能测准"有没有人在监听"。
port_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- && return 0
  return 1
}

port_pids() {
  local port="$1" pids=""
  pids="$(python3 "$(dirname "${BASH_SOURCE[0]}")/port_pids.py" "$port" 2>/dev/null | tr '\n' ' ' || true)"
  if [ -z "${pids// /}" ]; then
    pids="$(ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $NF}' \
            | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u | tr '\n' ' ' || true)"
  fi
  # 某些加固内核会隐藏 /proc/<pid>/fd 与 ss 的 PID 列（连自己的进程也读不到），
  # 于是按"是谁在跑我们的面板"兜底——这里只处理一个受管端口，收口是安全的。
  if [ -z "${pids// /}" ]; then
    pids="$(pgrep -f 'webpanel/server\.py' 2>/dev/null | tr '\n' ' ' || true)"
  fi
  printf '%s' "$pids"
}

# 结束占用端口的进程；**结束时再探测一次**，仍被占用就报错退出，
# 绝不"以为清干净了"就往下走（否则服务会因 bind 失败反复重启）。
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
    echo "[错误] 端口 $port 仍被占用，但拿不到占用进程的 PID。" >&2
    echo "       请先手动停掉它，例如： sudo fuser -k -n tcp $port" >&2
    return 1
  fi
  return 0
}

echo "[1/6] 迁移旧版单元（若存在）"
for legacy in "${LEGACY_UNITS[@]}"; do
  if [ -e "$UNIT_DIR/$legacy" ]; then
    echo "      发现旧单元 $legacy：停用并删除"
    "${SYSTEMCTL[@]}" disable --now "$legacy" 2>/dev/null || true
    rm -f "$UNIT_DIR/$legacy"
  fi
done
"${SYSTEMCTL[@]}" daemon-reload

echo "[2/6] 释放端口 $PANEL_PORT"
free_port "$PANEL_PORT" || exit 1

echo "[3/6] 生成并安装单元到 $UNIT_DIR"
mkdir -p "$UNIT_DIR"
sed -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
    -e "s|@RUN_USER@|$RUN_USER|g" \
    -e "s|@RUN_GROUP@|$RUN_GROUP|g" \
    -e "s|@PYTHON@|$PYTHON|g" \
    "$UNIT_SRC/$PANEL_UNIT.in" > "$UNIT_DIR/$PANEL_UNIT"
chmod 0644 "$UNIT_DIR/$PANEL_UNIT"
install -m 0644 "$UNIT_SRC/$TARGET_UNIT" "$UNIT_DIR/$TARGET_UNIT"

echo "[4/6] systemctl daemon-reload"
"${SYSTEMCTL[@]}" daemon-reload

echo "[5/6] enable --now $TARGET_UNIT"
"${SYSTEMCTL[@]}" enable --now "$TARGET_UNIT"

echo "[6/6] 状态"
sleep 2
"${SYSTEMCTL[@]}" --no-pager --lines=0 status "$TARGET_UNIT" || true

if [ "$MODE" = "user" ]; then
  JOURNAL="journalctl --user"
  UNINSTALL="./deploy/install-services.sh --uninstall --user"
else
  JOURNAL="journalctl"
  UNINSTALL="sudo ./deploy/install-services.sh --uninstall"
fi

# 面板监听地址：默认 127.0.0.1；若 panel.env 写了别的地址就照实显示
PANEL_HOST=127.0.0.1
if [ -f "$PANEL_ENV" ]; then
  HOST_LINE="$(grep -E '^[[:space:]]*PANEL_HOST[[:space:]]*=' "$PANEL_ENV" | tail -n1 || true)"
  if [ -n "$HOST_LINE" ]; then
    HOST_LINE="${HOST_LINE#*=}"; HOST_LINE="${HOST_LINE//[[:space:]\"]/}"
    [ -n "$HOST_LINE" ] && PANEL_HOST="$HOST_LINE"
  fi
fi

cat <<EOF

完成。常用命令：
  ${SYSTEMCTL[*]} status $TARGET_UNIT          # 整体状态
  ${SYSTEMCTL[*]} restart novelkit-panel       # 只重启面板
  $JOURNAL -u novelkit-panel -f                # 跟面板日志
  $UNINSTALL

面板：  http://$PANEL_HOST:$PANEL_PORT/

云服务器部署（令牌 / HTTPS 反代 / 防火墙）见 deploy/cloud.md。
EOF
