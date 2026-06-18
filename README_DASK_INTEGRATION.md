# Dask 分布式集群集成包

## 1. 已实现的功能

本集成包在现有“云和气溶胶反演系统”中增加固定的“分布式”管理页面，并保留原有本机多进程执行方式。

前端页面支持：

- 查看当前节点名称、IP、CPU、内存、操作系统、Python 路径及 Dask 版本；
- 一键安装 Dask Distributed；
- 一键配置 Windows 防火墙端口；
- 一键把当前节点创建为主节点；
- 自动启动 Dask Scheduler 和当前主节点 Worker；
- 显示主节点 IP、Scheduler 地址、Dashboard 地址、集群 ID 和加入令牌；
- 子节点输入主节点 IP 与加入令牌后，一键加入集群；
- 设置 Worker 数量、线程数和内存限制；
- 查看所有 Worker 的地址、名称、CPU、内存和任务数量；
- 在“本机执行 / 分布式执行”之间切换；
- 检测所有 Worker 是否能够访问共享运行目录；
- 查看 Scheduler、Worker 和安装日志；
- 一键离开集群、停止本机集群进程。

后端支持：

- 通过 Dask Future 分发原有 folder_chunks / batch 子任务；
- 算法仍以 subprocess 方式运行，兼容 Python 源码模块和 EXE 黑盒模块；
- 本机模式继续使用原 TaskManager；
- 分布式模式只传任务描述和小型 config.json，不通过 Dask 传输大型遥感数据；
- 远程节点、PID、返回码、日志和耗时回传到原任务管理页面；
- 使用共享取消标记文件终止远程 subprocess；
- Dask 不可用或未启用时不会影响本地任务。

## 2. 替换文件

先备份项目，然后将本包中的文件复制到本地项目同名位置：

```text
dask_integration_bundle/
├─ backend/
│  ├─ app/
│  │  ├─ main.py
│  │  ├─ task_manager.py
│  │  ├─ dask_cluster_manager.py
│  │  └─ dask_job_runner.py
│  └─ start_cluster_backend.bat
├─ frontend/
│  └─ src/
│     ├─ api.js
│     ├─ App.jsx
│     └─ assets/
│        └─ earth.jpg
├─ requirements_dask.txt
├─ install_dask_fallback.bat
└─ README_DASK_INTEGRATION.md
```

对应你的项目通常是：

```text
D:\local_web_module_system\backend\app
D:\local_web_module_system\frontend\src
```

本次不需要修改：

```text
backend/app/auth.py
backend/app/module_installer.py
backend/app/schemas.py
backend/app/store.py
frontend/src/main.jsx
frontend/src/styles.css
```

## 3. 重新构建前端

在每台电脑上执行：

```bat
cd /d D:\local_web_module_system\frontend
npm install
npm run build
```

## 4. 启动每个节点的系统后端

每台电脑都需要运行本系统后端，且必须监听局域网地址。

可将 `backend/start_cluster_backend.bat` 复制到项目的 `backend` 目录，然后执行。

当后端使用指定环境时，先在 BAT 中设置：

```bat
set "LOCAL_WEB_PYTHON_EXE=D:\envs\rayenv\python.exe"
```

环境名称叫 rayenv 不影响使用 Dask；真正使用的是该环境中的 Python。

## 5. 主节点操作

1. 登录主节点系统；
2. 点击工具栏中的“分布式”；
3. 点击“一键安装 Dask”；
4. 以管理员身份运行后端后，点击“配置防火墙”；
5. 填写 Worker 数量、线程数、内存限制；
6. 填写所有节点都可访问的共享运行目录；
7. 点击“创建主节点集群”；
8. 复制页面显示的主节点 IP 和加入令牌。

推荐共享目录使用 UNC：

```text
\\192.168.2.135\local_web_runtime
```

不要在分布式任务中使用只在主节点存在的路径，例如：

```text
D:\local_web_module_system\backend\runtime
```

## 6. 子节点加入

每台子节点都执行：

1. 启动相同版本的系统；
2. 打开“分布式”页面；
3. 点击“一键安装 Dask”；
4. 点击“配置防火墙”；
5. 输入主节点 IP、API 端口和加入令牌；
6. 设置该节点 Worker 数量、线程数和内存限制；
7. 点击“加入集群”。

加入后，主节点的 Worker 表会显示该节点。

## 7. 启用分布式任务

在主节点：

1. 点击“检测共享目录”；
2. 确认全部 Worker 的 `exists=true`、`is_dir=true`、`writable=true`；
3. 点击“启用分布式执行”。

之后原任务提交接口不变。`folder_chunks` 与批处理任务会由 Dask 调度到各个 Worker。

## 8. 重要前提

### 数据路径

所有 Worker 必须能够访问同一个输入、输出和共享运行路径。推荐：

```text
\\主节点IP\H8_DATA\input
\\主节点IP\H8_DATA\output
\\主节点IP\local_web_runtime
```

大型 NC/HDF/TIF 文件不会通过 Dask 对象传输。

### 模块路径

当前版本不会自动把算法、模型、DLL 或 Python 独立环境安装到其他节点。各节点必须提前具有：

- 相同版本的系统代码；
- 相同的 `installed_modules`；
- Python 模块所需的环境；
- EXE 所需的 DLL、模型和固定资源；
- 能够在该节点执行的模块路径。

最稳妥的第一阶段部署方式是让所有节点的项目根目录和模块目录保持一致，例如：

```text
D:\local_web_module_system
```

### 安全

该版本面向可信局域网。加入令牌用于避免普通误加入，但不是互联网级零信任安全方案。不要把 Scheduler、Dashboard 和后端 API 直接暴露到公网。

### 取消任务

分布式任务使用共享目录中的取消标记终止远程 subprocess。共享目录不可访问时，Dask Future 可以取消，但已经启动的外部 EXE 可能要运行到结束。

## 9. 常用端口

```text
8000  系统 FastAPI
8786  Dask Scheduler
8787  Dask Dashboard
```

如修改端口，请在页面和防火墙配置中保持一致。

## 10. 回退本地模式

在“分布式”页面点击“使用本机执行”即可。此操作不会删除 Dask，也不会影响节点连接，只会让新任务继续走原来的本机 TaskManager。
