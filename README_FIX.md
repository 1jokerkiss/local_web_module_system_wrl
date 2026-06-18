# Dask 分布式页面三个问题修复

## 修复内容

### 1. 普通用户也能使用“分布式”页面

- 顶部“分布式”按钮对 admin 和 user 都显示；
- 普通用户可以：
  - 查看节点和集群状态；
  - 安装 Dask；
  - 将当前电脑加入集群；
  - 让当前 Worker 退出集群；
  - 查看 Dashboard 和 Worker 状态；
- 管理员继续负责：
  - 创建或停止主节点；
  - 配置 Windows 防火墙；
  - 切换系统全局执行模式；
  - 检测共享目录。

### 2. 修复页面横向溢出

- 分布式页面主网格改为响应式 auto-fit；
- 节点信息、表单和日志区域会根据屏幕宽度自动变成单列；
- Card、Input、Pre 和表格容器增加 min-width/max-width/overflow 限制；
- 顶部工具栏允许自动换行；
- 页面不再因为右侧“创建主节点”区域产生整页横向滚动。

### 3. 修复子节点无法加入

原问题是主 FastAPI 只监听 127.0.0.1:8000，子节点访问：

```text
http://主节点IP:8000/api/distributed/join-info
```

会被拒绝。

修复后：

- 创建主节点时自动启动一个独立的局域网加入服务；
- 加入服务监听 `0.0.0.0`；
- 默认端口改为 `8790`；
- 不再要求主 FastAPI 使用 `--host 0.0.0.0`；
- 如果 8790 被占用，会自动尝试 8791～8799；
- 页面会显示实际“加入服务端口”；
- 子节点连接失败时会自动尝试用户填写端口、8790 和旧版 8000；
- 防火墙规则自动包含加入端口、8786 和 8787。

## 替换位置

```text
dask_three_issue_fix/backend/app/main.py
→ D:\local_web_module_system\backend\app\main.py

dask_three_issue_fix/backend/app/dask_cluster_manager.py
→ D:\local_web_module_system\backend\app\dask_cluster_manager.py

dask_three_issue_fix/frontend/src/App.jsx
→ D:\local_web_module_system\frontend\src\App.jsx
```

替换前请备份原文件。

## 替换后操作

1. 停止父节点和子节点后端；
2. 替换三个文件；
3. 重新构建前端：

```bat
cd /d D:\local_web_module_system\frontend
npm run build
```

4. 重新启动所有节点的后端；
5. 主节点进入“分布式”页面，先停止旧集群，再重新点击“创建主节点集群”；
6. 记录页面显示的：
   - 主节点 IP；
   - 加入服务端口，通常为 8790；
   - 加入令牌；
7. 子节点填入这三项并点击“加入集群”。

## 防火墙

主节点需要允许：

```text
8790-8799  集群加入服务
8786       Dask Scheduler
8787       Dask Dashboard
```

页面中的“配置 Windows 防火墙”按钮需要后端进程具有管理员权限。若自动规则失败，可以在管理员 CMD 中手工执行：

```bat
netsh advfirewall firewall add rule name="LocalWeb-Dask-Join" dir=in action=allow protocol=TCP localport=8790-8799
netsh advfirewall firewall add rule name="LocalWeb-Dask-Scheduler" dir=in action=allow protocol=TCP localport=8786
netsh advfirewall firewall add rule name="LocalWeb-Dask-Dashboard" dir=in action=allow protocol=TCP localport=8787
```

## 注意

旧主节点状态中可能仍保存 API 端口 8000。替换代码并重启后，建议重新创建一次主节点集群，使系统启用新的独立加入服务。
