# 自用 Claude 镜像框架 · 9443 版

此仓库从已运行的个人镜像整理而来。保留原来的侧栏账号入口、账号管理界面、流式接口和文件预览桥接。换服务器时，由启动器生成该实例的 HTTPS 入口和预览来源配置。它与 Anthropic 无隶属关系。

仓库提供自定义代码、安装器和 Docker Compose 配置。当前版的界面依赖已有的 Linux x86-64 运行程序；旧的文件预览还需要原版的脚本。**Git 仓库不含这两份外部资源，也不含服务器账号库、站点密码、证书或生产配置。**

## 安装

在装有 Docker Compose、Python 3.9+、OpenSSL 和 make 的 Linux x86-64 机器上，将你有权使用的两份原版文件放入：

| 路径 | 来源 |
| --- | --- |
| private/fuclaude | 与线上校验值一致的 Linux x86-64 程序。参考 [上游 v0.5.1 发行页](https://github.com/wozulong/fuclaude/releases/tag/v0.5.1)。 |
| private/frame-shell.js | 已部署版本中的文件预览运行脚本。 |

然后以普通用户运行：

~~~sh
make up
~~~

启动器会核对两份文件的 SHA-256，生成独立的输出目录 runtime/，启动三个组件。默认从本机打开 [https://127.0.0.1:9443/mirror/accounts](https://127.0.0.1:9443/mirror/accounts)，站点密码保存在 runtime/site-password。初次启动使用本机自签证书，新实例的账号库为空。默认只监听回环地址。

需要远程验收时，可以通过 SSH 本地端口转发。要直接对外开放，须提供入口与独立文件预览域名的有效证书，并只开放需要的端口。文件预览要使用与主站隔离的域名，避免用户内容与主站共享 Cookie；部署选项见 [deployment-target.md](deployment-target.md)。

~~~sh
make down
~~~

停止后，runtime/ 下的账号、站点设置和证书会保留。在受控环境迁移账号时应把状态和代码分开；不要把 runtime/ 或站点数据上传到 GitHub。

## 无 Docker 启动

在 Linux x86-64 上也可用标准 Nginx 和独立虚拟环境启动：

~~~sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python deploy/setup.py
.venv/bin/python deploy/run.py --nginx /usr/sbin/nginx
~~~

同机并行验证可以用 deploy/setup.py 的端口参数，选一组互不重叠的端口。在宝塔等环境里，启动器应使用标准 Nginx，而不是面板自带的定制程序。

## 校验

~~~sh
make test PYTHON=.venv/bin/python
~~~

manifest.json 记录整理前的线上文件与运行版本。verify_bundle.py 校验原始目录或原始补丁备份；本仓库的可移植代码包含参数化和空账号初始化改动，四个自定义文件以 Git 版本为准。首次发布前仍应使用独立测试账号在浏览器里实际验收流式回复、上传、生成预览和下载。
