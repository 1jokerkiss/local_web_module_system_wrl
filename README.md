# Dask Worker 结果无法回传修复

## 现象

任务日志停在：

```text
[DASK] 已提交 3/35
```

Scheduler 日志反复出现：

```text
Couldn't gather keys: {'local-web-...': 'memory'}
```

其中 `memory` 表示 Scheduler 认为任务结果已经保存在 Worker 内存中，
但主节点无法从 Worker 取回结果。

## 根因

原代码只开放：

- 8790：加入服务
- 8786：Scheduler
- 8787：Dashboard

但 `dask worker` 未指定 `--worker-port` 和 `--nanny-port`，因此每次启动都使用
随机高位端口。Windows 防火墙可能允许 Worker 主动连接 Scheduler，却阻止
Scheduler/Client 反向连接 Worker 获取结果。

## 修复

- Worker 固定使用 `9000:9099`
- Nanny 固定使用 `9100:9199`
- 所有节点自动添加上述端口范围的 Windows 防火墙规则
- 关闭不必要的 Worker Dashboard 随机端口
- `future.result()` 增加 45 秒结果回传超时和明确错误提示

## 替换文件

```text
backend/app/dask_cluster_manager.py
backend/app/task_manager.py
```

替换后必须在主节点和所有子节点上执行。

## 重启步骤

1. 停止当前任务。
2. 在主节点点击“停止集群”。
3. 停止所有节点后端。
4. 替换两个文件。
5. 以管理员权限启动每台电脑的后端。
6. 在每台电脑的“分布式”页面点击“配置 Windows 防火墙”。
7. 主节点重新创建集群。
8. 子节点重新加入。
9. 重新提交任务。

## 手工防火墙命令

每台电脑都以管理员身份执行：

```bat
netsh advfirewall firewall add rule name="LocalWeb-Dask-Worker" dir=in action=allow protocol=TCP localport=9000-9099 profile=any
netsh advfirewall firewall add rule name="LocalWeb-Dask-Nanny" dir=in action=allow protocol=TCP localport=9100-9199 profile=any
netsh advfirewall firewall add rule name="LocalWeb-Dask-Scheduler" dir=in action=allow protocol=TCP localport=8786 profile=any
```

## 验证

重新启动集群后，Scheduler 日志中的 Worker 地址应为：

```text
tcp://192.168.2.xxx:9000
tcp://192.168.2.xxx:9001
```

而不是随机的 `52746`、`59153`。

任务日志应继续出现：

```text
[DASK] 远程节点=...
[DASK] 完成 1/35
```
