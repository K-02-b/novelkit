# systemd 部署（NovelKit 面板）

一条命令装好面板服务：

```bash
cp webpanel/panel.env.example webpanel/panel.env   # 云端按需填 PANEL_TOKEN
sudo ./deploy/install-services.sh
```

> 路径是相对当前目录的。在 `~` 下直接敲 `sudo ./deploy/install-services.sh`
> 会报 `command not found`；用绝对路径 `sudo /path/to/novelkit/deploy/install-services.sh` 也可以。

脚本做的事：清理旧单元 → 释放端口 → 由 `novelkit-panel.service.in`
模板**按当前路径和当前用户**生成单元 → `daemon-reload` → `enable --now` → 报状态。

| 单元 | 作用 |
|---|---|
| `novelkit-panel.service` | 面板（安装时生成，含正确的绝对路径与运行用户） |
| `novelkit.target` | 服务组快捷方式，`systemctl enable --now novelkit.target` |

装好后：

```bash
sudo systemctl status novelkit.target          # 看整体
journalctl -u novelkit-panel -f                # 跟面板日志
sudo systemctl restart novelkit-panel          # 单独重启
sudo systemctl disable --now novelkit.target   # 全停并取消开机自启
sudo ./deploy/install-services.sh --uninstall  # 卸载
```

## 运行参数（webpanel/panel.env）

单元通过 `EnvironmentFile=-<项目>/webpanel/panel.env` 读取三个可选变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PANEL_HOST` | `127.0.0.1` | 监听地址。`0.0.0.0` 时必须同时设 `PANEL_TOKEN` |
| `PANEL_PORT` | `8897` | 监听端口 |
| `PANEL_TOKEN` | 空 | 访问令牌；非本机监听时**必填** |

改完 `panel.env` 执行 `sudo systemctl restart novelkit-panel` 生效。

## 用户级安装（没有 root 时）

```bash
./deploy/install-services.sh --user
```

前提：**这个用户必须有 systemd 会话**，也就是满足其中一条：

- 该用户有真实的登录会话（本地登录 / SSH 登录）；
- 或 root 执行过 `loginctl enable-linger <你>`（这样即使没人登录，用户服务也能常驻）。

判断能不能用：

```bash
systemctl --user status        # 报 "Failed to connect to bus" 就是还没有会话
```

用户级安装后命令要带 `--user`，例如 `systemctl --user status novelkit.target`。

## 云服务器

令牌、HTTPS 反向代理、防火墙与常见问题见 [`../cloud.md`](../cloud.md)。
