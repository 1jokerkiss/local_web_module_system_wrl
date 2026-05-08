本次修复说明

问题：
日志里 config.json 已经包含 GEO1_file，但 exe 仍然报 “JSON 缺少必要字段: GEO1_file”。
这通常说明 exe 实际没有读取命令行传入的那个 config.json，而是在读取 exe 所在目录或工作目录下的另一个 config.json。

修复：
1. JSON 配置模式下，每个 job 创建独立 runtime/job_xxx/module_work 目录。
2. 把模块目录里的 exe、deps、resources 等文件硬链接/复制到 module_work。
3. 把本 job 的 config.json 写到 module_work/config.json。
4. 命令里的 {executable} 改成 module_work 里的 exe。
5. cwd 也改成 module_work。
6. 这样 exe 无论读取：
   - 命令行参数 config.json
   - 当前目录 ./config.json
   - exe 所在目录 config.json
   都会读到当前 job 的配置。
7. 并行时每个 job 都有自己的 module_work，不会互相覆盖配置。

替换路径：
main.py              -> backend/app/main.py
App.jsx              -> frontend/src/App.jsx
api.js               -> frontend/src/api.js
schemas.py           -> backend/app/schemas.py
module_installer.py  -> backend/app/module_installer.py
store.py             -> backend/app/store.py
task_manager.py      -> backend/app/task_manager.py

替换后：
cd frontend
npm run build

然后重启后端，浏览器 Ctrl+F5。
