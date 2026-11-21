# 页面冒烟测试（手动，不属于 pytest）

用 jsdom 把**真实页面**连同**真实后端**跑起来（外部系统用假的），逐个点开「推荐」的概览 / 推荐歌单 / 任务 / 设置，
点保留、重试、选择候选、确认框、预览对话框、409/400 提示，并捕获所有脚本错误。改动 `src/web_static/index.html` 后建议跑一遍。

```bash
mkdir -p /tmp/jsd && cd /tmp/jsd && npm init -y && npm install jsdom@24        # 一次性；装在仓库之外
cd <仓库目录> && python tests/ui/ui_server.py > /tmp/ui_server.out &   # 输出的最后一行数字是端口
cd /tmp/jsd && cp <仓库目录>/tests/ui/ui_test.js . && node ui_test.js <端口>
```

期望输出全部 `✔` 且「脚本错误数: 0」。每次跑会改动造出来的数据（保留/重试/选择候选），重跑前请重启 `ui_server.py` 取干净状态。
停止后台服务时用锚定开头的匹配，避免 `pkill -f` 误杀当前 shell：`pkill -f "^python tests/ui/ui_server.py"`。

## 隔离（重要）

`ui_server.py` 已隔离：临时空配置（不读真实 `config.yaml`）、禁止访问外网、通知置空、假 LB 的 `love` 一旦被调用即报错。
新增类似脚本时务必同样处理——曾因没有隔离，跑一次就给用户手机推送了一堆真实的 Bark 通知。
