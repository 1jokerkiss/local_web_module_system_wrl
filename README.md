# 本地模块 Web 系统（FastAPI + React）

这是一个可在本机运行的简易模块平台，满足这些核心需求：

- 浏览器打开本地网页使用
- 管理员可添加模块
- 用户可看到模块按钮并填写参数
- 点击运行后弹出任务窗口
- 任务窗口可关闭（相当于最小化），任务继续后台运行
- 可对同一模块重复提交不同参数，形成多个并行任务
- 支持多个模块同时执行
- 支持顺序工作流与并行工作流
- 你的本地 C/C++/Python/EXE 模块都可以作为外部命令接入

## 一、推荐运行方式

### 1）启动后端
```bash
cd backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 2）启动前端
```bash
cd frontend
npm install
npm run dev
```

浏览器打开：
- 前端：http://127.0.0.1:5173
- 后端：http://127.0.0.1:8000/docs

## 二、模块接入思路

系统把你的本地模块当成“可执行任务”来运行。每个模块只需要配置：

- `id`：模块唯一标识
- `name`：模块名称
- `description`：说明
- `executable`：可执行程序路径
- `working_dir`：工作目录
- `command_template`：命令模板
- `inputs`：前端表单字段
- `config_mode`：是否自动把表单输入写成 JSON 文件再传给模块

### AOD_AHI 模块特别说明

你上传的 `AOD_AHI(2).cpp` 已经支持：

```bash
AOD_AHI.exe config.json
```

也支持 9 个位置参数模式，并且会校验输入文件存在性。  
这正好适合本系统的 `json_file` 模式。相关入口可见上传源码中的 `main()`、`loadConfigFromJson()` 和 `validateInputs()`。fileciteturn0file0L79-L176 fileciteturn0file0L632-L703

## 三、把你的 C++ 模块接进来

### 方案 1：你已经有可执行文件
例如：
```bash
D:\modules\AOD_AHI.exe
```

那就在“管理员页面”新增模块，填：

- executable: `D:/modules/AOD_AHI.exe`
- config_mode: `json_file`
- command_template:
```json
["{executable}", "{config_path}"]
```

### 方案 2：你现在只有源码
先自行编译出 EXE 再接入。  
因为你这份源码依赖 GDAL / HDF5 头文件和库，例如头文件中已经声明了这些依赖。fileciteturn0file2L1-L7

## 四、默认账号说明

这是本地单机简版：
- 默认不做复杂登录
- 管理员操作使用请求头 `X-Admin-Token`
- 默认 token：`admin123`

前端右上角可切换管理员模式并录入 token。

## 五、任务执行模式

### 1）单任务执行
一个模块一次输入一套参数，产生一个任务。

### 2）并行执行
对同一模块或者不同模块重复提交，系统会并发拉起多个子进程。

### 3）顺序工作流
按你设定的模块队列顺序，一个结束后再启动下一个。

### 4）并行工作流
一次发多个步骤，同时启动。

## 六、任务最小化说明

前端里的“任务窗口”本质是一个可关闭的任务详情弹窗。  
关闭后任务仍在后台运行，右下角任务托盘仍显示状态，随时可重新打开。

## 七、目录说明

```text
local_module_web_system/
  backend/
    app/
    data/
    modules/
  frontend/
```

## 八、下一步接你的真实模块

你先把系统跑起来。  
然后你只需要做两件事：

1. 把你的 AOD 模块编译成 exe
2. 在管理员页按示例新增模块

系统就能直接调你的本地代码跑。
