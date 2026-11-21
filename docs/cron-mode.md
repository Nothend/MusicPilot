# 旧版 cron 批处理模式（可选）

> 项目最初是一个纯定时脚本：每天扫描「名字等于当天日期」的歌单并下载。现在更推荐用 [Web 控制台](web-console.md)，这里保留旧模式的完整说明。

## 前置准备

1. **黑胶会员账号**（VIP）。
2. **Cookie**：登录网易云音乐网页版 → 按 F12 打开开发者工具 → Network 标签页 →
   刷新后点任意一条 `music.163.com` 请求 → 复制其请求头里的 `Cookie` 值。
   必须包含 `MUSIC_U`，形如 `MUSIC_U=xxxx;os=pc;appver=8.9.75`（**填进配置时不要加引号**）。
3. **uid**：你的网易云用户 ID（打开自己的个人主页，地址栏 `user/home?id=` 后面那串数字）。

## 快速开始（Docker Compose）

1. 新建一个目录，放入 `docker-compose.yml` 和 `config.yaml`（两者需在同一目录）。

   `docker-compose.yml`：

   ```yaml
   services:
     autoMusicDown:
       image: leonautilus/auto-music-downloader:latest
       container_name: auto-music-downloader
       restart: always
       volumes:
         - ./config.yaml:/app/config.yaml:ro    # 配置（只读）
         - ./logs:/app/logs                     # 日志
         - ./downloads:/app/downloads           # 下载目录（可映射到 music-tag-web 的下载目录）
       environment:
         - TZ=Asia/Shanghai                     # 时区（日期歌单匹配依赖本地时区，务必设对）
         - CRON_SCHEDULE=0 20-23,0 * * *        # 执行时间：每天 20-23 点及 0 点，每小时一次
         # - RUN_ON_START=true                  # 可选：容器启动即先同步一次（默认 false）
   ```

2. 参照下方「配置详解」写好 `config.yaml`（可从仓库的 `config.example.yaml` 复制起步）。

3. 启动：

   ```bash
   docker compose up -d
   docker compose logs -f        # 观察运行日志
   ```

4. **日常使用**：每天在云音乐客户端把喜欢的每日推荐歌曲，加入一个**名字正好是当天日期**
   （如 `20251025`）的歌单即可，剩下的交给定时任务。

## 配置详解（`config.yaml`）

| 配置项 | 说明 | 默认/示例 |
|---|---|---|
| `cookie` | 网易云登录 Cookie，须含 `MUSIC_U`；**不要加引号** | `MUSIC_U=xxx;os=pc;appver=8.9.75` |
| `uid` | 你的网易云用户 ID | `123456` |
| `LEVEL` | 日志级别（`DEBUG`/`INFO`/`WARNING`/`ERROR`） | `INFO` |
| `QUALITY_LEVEL` | 下载音质等级，见下表 | `lossless` |
| `NAVIDROME.USE_NAVIDROME` | 是否启用 Navidrome 库去重 | `false` |
| `NAVIDROME.NAVIDROME_HOST` | Navidrome 地址（带协议，如 `http://192.168.0.6:4533`） | — |
| `NAVIDROME.NAVIDROME_USER` / `NAVIDROME_PASS` | Navidrome 账号/密码（用 Subsonic token 认证，密码不进 URL/日志） | — |
| `music-tag-web.USE_MYSQL` | 是否启用 music-tag-web 的 MySQL 库去重 | `false` |
| `music-tag-web.host` / `port` / `user` / `password` / `database` | music-tag-web 使用的 MySQL 连接信息 | `3306` / `music_tag` 等 |
| `BARK_API` | 可选，Bark 推送地址；留空则不推送 | `https://api.day.app/your_key/` |
| `REQUEST_DELAY` | 可选，处理每首歌之间的间隔秒数，降低被限流概率；想更快可设 `0` | `0.5` |

> **去重后端二选一**：`NAVIDROME` 与 `music-tag-web` 同时开启时，优先使用 Navidrome；
> 两者都不开则只做本地文件去重。

### 音质等级

| 值 | 含义 |
|---|---|
| `standard` | 标准 |
| `exhigh` | 极高 |
| `lossless` | 无损（推荐） |
| `hires` | Hi-Res |
| `sky` | 沉浸环绕声 |
| `jyeffect` | 高清环绕声 |
| `jymaster` | 超清母带 |

## 环境变量（`docker-compose.yml`）

| 变量 | 说明 | 默认 |
|---|---|---|
| `TZ` | 容器时区。**日期命名的歌单匹配依赖本地时区**，跨零点时尤其重要 | `Asia/Shanghai` |
| `CRON_SCHEDULE` | 执行 `main.py` 的 cron 表达式 | `0 20-23,0 * * *`（每天 20-23 点及 0 点整点各一次） |
| `RUN_ON_START` | 设为 `true` 时容器启动立即先同步一次；否则只等定时触发 | `false` |

## 去重与格式说明

- **库里的 MP3 视为「不存在」**：若音乐库里某首只有 MP3 版本，工具仍会去下载它的无损版本
  （库统一无损）。
- **只有 MP3 可下的歌会被跳过**：网易云只返回 MP3 时，直接跳过该曲并省去后续详情/歌词请求。
- **本地去重按文件名**：以「`歌手&歌手 - 歌名`」为文件名判断本地是否已存在，命中即跳过。

## 目录与日志

- **下载目录**：容器内 `/app/downloads`（映射到宿主机 `./downloads`）。可直接指向
  music-tag-web 的下载目录，便于统一管理。
- **日志**：容器内 `/app/logs/<日期>.log`（映射到宿主机 `./logs`）；每轮任务的运行输出
  同时可在 `docker compose logs` 里看到。
