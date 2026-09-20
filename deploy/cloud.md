# 云服务器部署指南

面板既可以只在本机跑，也可以部署到云服务器（VPS / 家里的小主机都适用）。
两种方式的区别只有一处：**面向公网时必须启用访问令牌，并建议再套一层 HTTPS 反向代理**。

> 面板能读到你的模型 API Key（等同账号密码），
> 所以**不要**在没有令牌、没有 HTTPS 的情况下把它直接暴露到公网。

---

## 0. 选哪种拓扑

| 拓扑 | 适用 | 做法 |
| --- | --- | --- |
| **A. 只监听本机 + Nginx 反代**（推荐） | 有域名、想上 HTTPS | `PANEL_HOST=127.0.0.1`，Nginx 监听 80/443 转发到 8897 |
| **B. 直接监听公网 + 令牌** | 只想用 `IP:端口` 快速访问 | `PANEL_HOST=0.0.0.0` + `PANEL_TOKEN=随机串` |
| **C. SSH 隧道** | 不想开放任何端口 | `PANEL_HOST=127.0.0.1`，本地 `ssh -L 8897:127.0.0.1:8897 user@server` |

> 无论哪种，只要监听的不是 `127.0.0.1`，面板就**强制要求** `PANEL_TOKEN`；
> 没设令牌会拒绝启动（fail-closed），避免误把带 API Key 的面板裸奔到公网。

---

## 1. 准备服务器

Ubuntu / Debian 为例（CentOS 把 `apt` 换成 `dnf`）：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

## 2. 拉取代码并安装依赖

```bash
git clone <你的仓库地址> ~/novelkit
cd ~/novelkit

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 可选：先跑一遍离线自检（不联网）
.venv/bin/python tests/test_novelkit.py
```

## 3. 配置密钥（.env）

在**项目根目录**（`git clone` 出来的目录）新建 `.env`：

```bash
cat > .env <<'EOF'
API_KEY=sk-你的模型密钥
API_BASE_URL=https://api.deepseek.com
API_MODEL=deepseek-flash
EOF
chmod 600 .env
```

启动面板后在「环境配置」页可以直接改这三个键（写入同一个 `.env`，改前自动备份、改后权限 600），
也可以点「验证」向模型服务发一次最小请求确认配置可用。

> `.env` 已被 `.gitignore` 忽略，**不会被提交**。若误提交过，请立刻轮换密钥。

## 4. 设置访问令牌（panel.env）

```bash
cp webpanel/panel.env.example webpanel/panel.env

# 生成一个随机令牌并写进去
TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
sed -i "s|^#\?PANEL_TOKEN=.*|PANEL_TOKEN=$TOKEN|" webpanel/panel.env
chmod 600 webpanel/panel.env

echo "访问令牌：$TOKEN"     # 记下来，登录面板要用
```

按需要选择监听地址：

- **拓扑 A / C**：`PANEL_HOST=127.0.0.1`（默认，推荐）
- **拓扑 B**：`PANEL_HOST=0.0.0.0`，并确认云厂商安全组只放行你需要的端口

## 5. 用 systemd 常驻

```bash
sudo ./deploy/install-services.sh
```

脚本会：按当前路径生成 `novelkit-panel.service`（以项目目录所有者的身份运行）、
安装 `novelkit.target`、释放端口、`enable --now` 并打印状态。
它读取 `webpanel/panel.env`，所以**先做完第 4 步再安装**。

> 作品默认放在项目下的 `works/`（可写即可）。数据盘容量更大时，用
> `NOVELKIT_WORKSPACE=/data/novels` 把工作区挪过去，并在 systemd 单元里
> 加上对应的 `Environment=` 或用 `systemctl edit` 覆盖。

```bash
sudo systemctl status novelkit.target     # 整体状态
journalctl -u novelkit-panel -f           # 跟日志
sudo systemctl restart novelkit-panel     # 重启
```

> 没有 systemd（或没有 root）时，可用过渡脚本：
> `./deploy/services.sh start|stop|restart|status`（用 `setsid` 放后台）。

## 6. Nginx + HTTPS（拓扑 A 推荐）

```bash
sudo apt install -y nginx
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/novelkit
sudo sed -i 's/panel.example.com/你的域名/' /etc/nginx/sites-available/novelkit
sudo ln -sf /etc/nginx/sites-available/novelkit /etc/nginx/sites-enabled/novelkit
sudo nginx -t && sudo systemctl reload nginx

# 一键申请证书并自动改写成 443
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 你的域名
```

`X-Forwarded-Proto` 会被面板识别：走 HTTPS 时登录 Cookie 自动带 `Secure`。

## 7. 防火墙

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
# 拓扑 B 才需要放行面板端口；拓扑 A 不要放行 8897
sudo ufw enable
```

云厂商的**安全组**也要同步：只放 22 / 80 / 443，不要放 8897。

## 8. 首次登录

浏览器打开 `http://你的域名/`（或 `http://IP:端口/`），会看到登录页。
如果用的是 `?token=` 一次性链接（`http://你的域名/?token=<令牌>`），
面板会写入一个 HttpOnly Cookie 并跳回首页，之后正常访问即可，不用每次带令牌。

登录状态存在浏览器 Cookie 里；换浏览器或清了 Cookie 就再登一次。
面板「环境配置」页有「退出登录」。

---

## 可选：安装作品导出工具（第三方）

面板**不提供**任何内容获取功能，侧栏的「工具」只是**同源反向代理**
（`/tools/tomato/` → `webpanel/tools.json` 里配置的地址，默认 `127.0.0.1:18423`），
转发到你**自行安装**的第三方程序，用来把自己作品的原文导出成本地文本。

> ⚠️ **合规红线**：只允许处理你**自己创作**、或**已获授权 / 属于公有领域**的作品。
> 下载、翻译、传播他人作品可能侵犯著作权，后果由使用者自负；本项目不分发、
> 不托管任何第三方内容，也不对使用者的行为负责。

下面是该第三方项目（MIT 许可，作者 [@zhongbai2333](https://github.com/zhongbai2333)）
的安装方式，**不附带、不内嵌、不修改**，请从上游官方 Releases 自行下载
[Tomato-Novel-Downloader](https://github.com/zhongbai2333/Tomato-Novel-Downloader)：

```bash
mkdir -p ~/.local/share/tomato && cd ~/.local/share/tomato

# Linux x86_64；其它平台换成 Releases 里对应的文件名（版本号也可换最新）
wget -O TomatoNovelDownloader \
  https://github.com/zhongbai2333/Tomato-Novel-Downloader/releases/download/v2.4.15/TomatoNovelDownloader-Linux_amd64-v2.4.15
chmod +x TomatoNovelDownloader

# 后台常驻：只监听本机，交给面板反代；**不要**把它绑到 0.0.0.0
TOMATO_WEB_ADDR=127.0.0.1:18423 TOMATO_WEB_PASSWORD='换成你的密码' \
  setsid nohup ./TomatoNovelDownloader --server --data-dir "$PWD" \
  > "$PWD/webui.log" 2>&1 &

# 之后在面板侧栏点「作品导出工具」即可，实际访问 /tools/tomato/
```

也可以改用上游的一键安装脚本或 Docker：

```bash
bash <(curl -sL https://raw.githubusercontent.com/zhongbai2333/Tomato-Novel-Downloader/main/installer.sh)

docker run -d --name tomato-novel-webui \
  -p 127.0.0.1:18423:18423 \
  -v /srv/tomato:/data \
  -e TOMATO_WEB_ADDR=0.0.0.0:18423 \
  -e TOMATO_WEB_PASSWORD=你的密码 \
  zhongbai233/tomato-novel-downloader-webui:latest --server --data-dir /data
```

> Docker 例子里的 `TOMATO_WEB_ADDR=0.0.0.0:18423` 是**容器内**地址，
> 对外仍被 `-p 127.0.0.1:18423:18423` 限制在服务器本机，所以是安全的。

反代的工作方式与限制见 [`../webpanel/README.md`](../webpanel/README.md#外部工具反代toolsid)：
改写 `/assets/`、`/api/`、`/download/`、`/download-zip/` 等根绝对前缀，
只允许打到配置的同一台主机，面板访问令牌就是它的门禁。不想用它就加
`--no-tool-proxy`（或 `PANEL_TOOL_PROXY=0`）。

注意事项（来自该项目的说明，请遵守）：

- 请不要在使用它时挂 VPN / 网络代理，也不要随意加大并发线程数；
- 下载到的小说仅供自行阅读，请勿转载或用于侵权用途；
- 若要把它的 WebUI 暴露到公网，请放在反向代理 / HTTPS 后面并**务必开启密码锁**。

许可与版权归原作者所有；本项目只做反代/跳转，出现问题请到上游仓库反馈。
合规风险（翻译权、发布、商用）见根目录 README 的「使用须知与合规」。

---

## 安全清单

- [ ] `.env` 与 `webpanel/panel.env` 权限 `600`，且都在 `.gitignore` 里
- [ ] 公网监听时设置了 `PANEL_TOKEN`（否则面板拒绝启动）
- [ ] 生产环境走 HTTPS（Nginx + certbot）
- [ ] 安全组 / 防火墙只放 22、80、443，不放 8897
- [ ] 定期轮换模型 API Key
- [ ] 分享日志前先脱敏：`log/`、`nohup.out` 里有完整提示词与译文
- [ ] 第三方下载器只监听 `127.0.0.1:18423`（不绑 `0.0.0.0`），只经由面板反代访问

## 常见问题

**面板启动即退出，日志说"监听非本机地址时必须设置访问令牌"**
→ 在 `webpanel/panel.env` 里设置 `PANEL_TOKEN`，或把 `PANEL_HOST` 改回 `127.0.0.1`。

**侧栏「作品导出工具」打不开 / 白屏**
→ 面板反代是服务端转发，先在服务器上确认下载器在跑：
`curl -sI http://127.0.0.1:18423/ | head -1`。
若下载器改了端口，到「环境配置 → 外部工具地址」改成新地址。
用了 WebSocket 的界面不受支持（本反代只做 HTTP）。

**打开页面一直跳登录页 / 401**
→ 令牌不对；用 `http://域名/?token=<令牌>` 重新打开一次。
反向代理若没透传 `Host`，也可能导致 Cookie 域不符，确认 `proxy_set_header Host $host;`。

**上传 EPUB 报 413**
→ Nginx 的 `client_max_body_size` 调大（示例配置已给 100m）。

**「环境配置」点验证提示调用失败**
→ 多半是 API Key 填错、余额不足或地址写错；面板只回显错误类型与消息，不会显示密钥本身。
