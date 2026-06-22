# Dask 自动分布式执行修复

本修复解决：

1. 集群在线但任务仍静默使用本机进程池；
2. 用户选择 distributed 后 Scheduler/Worker 离线时自动回退本机；
3. 创建主节点后忘记手动点击“启用分布式任务调度”；
4. Worker 使用 auto 内存限制过低。

替换文件：

- backend/app/dask_cluster_manager.py
- backend/app/task_manager.py
- frontend/src/App.jsx

替换后执行：

```bat
cd /d D:\local_web_module_system\frontend
npm run build
```

然后重启所有节点后端。

推荐参数：

- 每台电脑 Worker 进程数：1
- 每个 Worker 线程数：1
- 每个 Worker 内存限制：4GB（内存不足的电脑可改 2GB）
- 共享运行目录：所有节点可读写的 UNC 路径

创建主节点时，前端会：
1. 强制要求共享目录；
2. 创建 Scheduler 和主节点 Worker；
3. 检测所有 Worker 的共享目录；
4. 检测通过后自动切换为 distributed。

任务日志判断：
- `[DASK]`：使用集群
- `[BACKEND] 当前任务使用本机进程池`：本机执行
- `[DASK-ERROR]`：已选择分布式，但集群不可用，任务失败且不会静默回退本机
