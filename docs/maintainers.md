# 维护者指南

## 打包与发布

以下命令都在项目根目录（有 `Dockerfile` 的那一层）执行。镜像代码来自**当前工作区**（`COPY src/`），
未提交的改动也会被打进去，所以「本地打包」不需要先提交。

### 一、本地打包镜像（导出 tar.gz，上传到 NAS / 群晖 / 绿联等离线部署）

```bash
VERSION=1.1.2                      # ← 改成新版本号；上一版见 ls dist/，递增即可
PLATFORM=linux/amd64               # x86 NAS（群晖多数型号、绿联 DXP/DH 系列）；ARM 机型改成 linux/arm64

# 1) 构建
docker build --platform $PLATFORM -t auto-music-downloader-web:$VERSION .

# 2) 导出并压缩（dist/ 已在 .gitignore 与 .dockerignore 中，不会被提交，也不会被打进镜像）
mkdir -p dist
ARCH=${PLATFORM#linux/}            # amd64 / arm64
docker save auto-music-downloader-web:$VERSION | gzip -9 > dist/auto-music-downloader-web-$VERSION-$ARCH.tar.gz

# 3) 生成校验值（上传后可用 sha256sum -c 核对文件是否完整）
(cd dist && sha256sum auto-music-downloader-web-$VERSION-$ARCH.tar.gz > auto-music-downloader-web-$VERSION-$ARCH.tar.gz.sha256)
ls -lh dist/
```

产物：`dist/auto-music-downloader-web-<版本>-<架构>.tar.gz`（约 30MB）及其 `.sha256`。

**部署端导入**：NAS 的 Docker 界面 →「镜像」→「本地导入 / 从文件添加」选该 tar.gz；
或在 NAS 上执行 `docker load -i auto-music-downloader-web-<版本>-<架构>.tar.gz`。
导入后镜像名为 `auto-music-downloader-web:<版本>`，compose 里的 `image:` 要写成同名。
可参考 `dist/` 下的示例：`docker-compose.ugreen.yml`（绿联）、`docker-compose.synology.yml`（群晖），
发新版时把示例里的版本号改成新版本。**数据目录沿用旧路径，登录、设置、定时任务和任务记录都会保留。**

> **架构要和目标机一致**：在 x86 机器上构建 amd64 包最稳；构建 arm64 包需要 buildx/QEMU
> （`docker buildx build --platform linux/arm64 --load ...`）。

**导出前建议本机冒烟一下**（端口 15119，用完删除）：

```bash
mkdir -p /tmp/amd-run/data /tmp/amd-run/downloads
docker run -d --name amd-test -p 15119:5119 -e WEB_ENABLE=true -e WEB_PORT=5119 -e WEB_PASSWORD=test \
  -e CRON_ENABLE=false -v /tmp/amd-run/data:/app/data -v /tmp/amd-run/downloads:/app/downloads \
  auto-music-downloader-web:$VERSION
sleep 4 && docker logs amd-test | tail -3
curl -s -o /dev/null -w '无密码: %{http_code}\n' http://127.0.0.1:15119/          # 期望 401
curl -s -u admin:test 'http://127.0.0.1:15119/api/jobs?limit=1'                  # 期望 JSON
docker rm -f amd-test && rm -rf /tmp/amd-run
```

**常见问题**：构建时拉基础镜像报 `401 Unauthorized: incorrect username or password` ——
本机保存的 Docker Hub 登录失效了。先 `docker login` 重新登录；
或临时用不带登录的配置构建：`mkdir -p /tmp/dockercfg-anon && echo '{}' > /tmp/dockercfg-anon/config.json`，
然后命令前加 `DOCKER_CONFIG=/tmp/dockercfg-anon`（只影响这一条命令）。

### 二、发布到 Docker Hub（`leonautilus/auto-music-downloader`）

有两种方式，**推荐方式 A**（可复现，镜像来自已提交的代码）。

**方式 A：推 tag，GitHub Actions 自动构建并推送**（`.github/workflows/docker-image.yml`，
需要仓库里已配置 Secrets：`DOCKER_HUB_USERNAME`、`DOCKER_HUB_ACCESS_TOKEN`）

```bash
git add -A && git commit -m "feat/fix: 本次改动说明"     # 代码必须先提交并推送，CI 构建的是该 tag 指向的提交
git push origin main
git tag v1.1.3                                           # 版本号：先 git tag --sort=-v:refname | head -3 看最新的，再递增
git push origin v1.1.3                                   # 推 tag 即触发构建，同时推送 :1.1.3 和 :latest 两个标签
```

在 GitHub 仓库的 Actions 页确认运行成功（**成功才算发布完成**；失败时镜像不会更新）。
注意：标签规则是 `type=semver`，所以 **tag 必须形如 `vX.Y.Z`**，镜像标签会去掉前面的 `v`（`v1.1.2` → `1.1.2`）。

**方式 B：本机直接推送**（不依赖 GitHub，适合 CI 不可用时；用本机当前工作区代码构建）

```bash
docker login                                             # 登录 Docker Hub（用户名 leonautilus；密码用 Access Token）
VERSION=1.1.3                                            # 与 git tag 保持一致（不带 v）
docker build --platform linux/amd64 -t leonautilus/auto-music-downloader:$VERSION .
docker tag leonautilus/auto-music-downloader:$VERSION leonautilus/auto-music-downloader:latest
docker push leonautilus/auto-music-downloader:$VERSION
docker push leonautilus/auto-music-downloader:latest
```

方式 B 不会创建 git tag。之后若再对同一版本推 `v$VERSION` tag，CI 会重新构建并**覆盖**同名镜像，
所以用方式 B 发布后，要么不再推该 tag，要么确保已提交的代码和刚才构建的一致。

> **版本号**：从 v1.1.2 起，离线包版本、git tag、Docker Hub 标签统一用同一个号（此前 Docker Hub 用 3.x.x，此后以 `latest` 和 `1.1.2` 起为准）。

**发布后核对**：

```bash
docker pull leonautilus/auto-music-downloader:latest
docker image inspect leonautilus/auto-music-downloader:latest --format '{{.Os}}/{{.Architecture}} {{.Created}}'
```

用户端升级见上文「更新与回滚」（`docker compose pull && docker compose up -d`）。

## 开发与测试

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements-dev.txt     # 运行依赖 + pytest；venv/ 已在 .gitignore
./venv/bin/python -m pytest tests -q                                       # 500+ 个测试，约 30 秒，不联网
```

- 项目是扁平导入：测试里 `tests/conftest.py` 已把 `src/` 放进路径；手动运行 Web 服务要 `cd src && python web_server.py`。
- **测试绝不能碰真实服务**：`conftest.py` 里有两道自动生效的保护——**网络闸门**（任何访问非本机地址的 DNS/连接都会让测试失败，因为 Bark 之类的代码会吞掉自己的异常）和**配置隔离**（`CONFIG_PATH` 指向临时空配置，避免读到真实的 `config.yaml`）。曾经因为没隔离，跑一次测试就给手机推送了上百条真实 Bark 通知。新写的、会创建真实 `App` 的脚本（例如 `tests/ui/ui_server.py`）也必须这样隔离。
- 页面改动后建议跑一遍页面冒烟测试（jsdom 加载真实页面 + 真实后端 + 假的外部系统，会点遍各个子页面和按钮并捕获脚本错误），见 `tests/ui/README.md`。
