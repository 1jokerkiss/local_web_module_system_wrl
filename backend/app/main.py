from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from string import Formatter
from typing import Any, Dict, List, Optional
from datetime import datetime
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import (
    admin_reset_password,
    create_token,
    create_user,
    delete_user,
    get_current_user,
    get_security_question,
    load_users,
    register_user,
    remove_token,
    require_admin,
    reset_password_by_security_answer,
    sanitize_user,
    update_user_enabled,
    update_user_role,
    verify_user,
)
from .task_manager import TaskManager


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

MODULES_FILE = DATA_DIR / "modules.json"
TASKS_FILE = DATA_DIR / "tasks.json"

INSTALLED_MODULES_DIR = BASE_DIR / "installed_modules"
INSTALLED_MODULES_DIR.mkdir(parents=True, exist_ok=True)

RUNTIME_DIR = BASE_DIR / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

FRONTEND_DIST_DIR = BASE_DIR.parent / "frontend" / "dist"

app = FastAPI(title="云和气溶胶反演系统 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

task_manager = TaskManager(TASKS_FILE)


# =========================
# 数据模型
# =========================
class LoginRequest(BaseModel):
    username: str
    password: str
    role: Optional[str] = None


class RegisterRequest(BaseModel):
    username: str
    password: str
    security_question: str = ""
    security_answer: str = ""


class ForgotPasswordResetRequest(BaseModel):
    username: str
    answer: str
    new_password: str


class AddUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    security_question: str = ""
    security_answer: str = ""


class UpdateUserRoleRequest(BaseModel):
    role: str


class UpdateUserEnabledRequest(BaseModel):
    enabled: bool
class ResetUserPasswordRequest(BaseModel):
    new_password: str
class ModuleRunRequest(BaseModel):
    module_id: str
    inputs: Dict[str, Any] = {}
class ModuleSaveRequest(BaseModel):
    id: str
    name: str
    description: str = ""
    executable: str
    working_dir: str = "."
    config_mode: str = "none"
    command_template: List[str] = []
    inputs: List[Dict[str, Any]] = []
    tags: List[str] = []
    enabled: bool = True
# 通用辅助函数
def ensure_modules_file():
    if not MODULES_FILE.exists():
        MODULES_FILE.write_text("[]", encoding="utf-8")
def load_modules() -> List[dict]:
    ensure_modules_file()
    try:
        data = json.loads(MODULES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []

def sanitize_filename(name: str) -> str:
    name = Path(name).name
    return name.replace("..", "_").replace("/", "_").replace("\\", "_")

def save_modules(modules: List[dict]):
    MODULES_FILE.write_text(
        json.dumps(modules, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_module(module_id: str) -> Optional[dict]:
    for module in load_modules():
        if module.get("id") == module_id:
            return module
    return None

@app.get("/api/files")
def api_list_user_files(authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    user_dir = UPLOADS_DIR / user["username"]
    user_dir.mkdir(parents=True, exist_ok=True)

    items = []
    for p in sorted(user_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file():
            stat = p.stat()
            items.append({
                "name": p.name,
                "path": str(p.resolve()),
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    return items


@app.post("/api/files/upload")
def api_upload_user_file(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    user = get_current_user(authorization)
    user_dir = UPLOADS_DIR / user["username"]
    user_dir.mkdir(parents=True, exist_ok=True)

    original_name = sanitize_filename(file.filename or "uploaded_file")
    target = user_dir / original_name

    # 如重名，自动追加编号
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        idx = 1
        while True:
            candidate = user_dir / f"{stem}_{idx}{suffix}"
            if not candidate.exists():
                target = candidate
                break
            idx += 1

    with target.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    return {
        "ok": True,
        "name": target.name,
        "path": str(target.resolve()),
        "size": target.stat().st_size,
    }


@app.delete("/api/files/{filename}")
def api_delete_user_file(filename: str, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    user_dir = UPLOADS_DIR / user["username"]
    user_dir.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(filename)
    target = user_dir / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    target.unlink()
    return {"ok": True}


@app.get("/api/files/{filename}/download")
def api_download_user_file(filename: str, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    user_dir = UPLOADS_DIR / user["username"]
    user_dir.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(filename)
    target = user_dir / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(str(target), filename=target.name)

def upsert_module(module_data: dict):
    modules = load_modules()
    found = False
    for i, module in enumerate(modules):
        if module.get("id") == module_data.get("id"):
            modules[i] = module_data
            found = True
            break
    if not found:
        modules.append(module_data)
    save_modules(modules)


def remove_module(module_id: str) -> bool:
    modules = load_modules()
    new_modules = [m for m in modules if m.get("id") != module_id]
    if len(new_modules) == len(modules):
        return False
    save_modules(new_modules)
    return True


def format_command(template: List[str], values: Dict[str, Any]) -> List[str]:
    formatted = []
    for item in template:
        try:
            formatted.append(item.format(**values))
        except KeyError as e:
            raise HTTPException(status_code=400, detail=f"命令模板缺少参数: {e}")
    return formatted


def extract_template_fields(command_template: List[str]) -> List[str]:
    fields = set()
    for item in command_template:
        for _, field_name, _, _ in Formatter().parse(item):
            if field_name:
                fields.add(field_name)
    return list(fields)


def build_runtime_for_module(module: dict, inputs: Dict[str, Any]) -> tuple[list[str], str, dict]:
    import os
    import json
    import tempfile
    from pathlib import Path

    executable = module.get("executable") or module.get("entry") or ""
    working_dir = module.get("working_dir", ".")
    config_mode = (module.get("config_mode") or "none").lower()
    command_template = module.get("command_template") or []

    if not executable:
        raise HTTPException(status_code=400, detail="模块未配置 executable")

    # 动态获取项目根目录，即 local_module_web_system 文件夹
    # BASE_DIR 是 backend 目录，所以 BASE_DIR.parent 就是项目根目录
    project_root = BASE_DIR.parent

    # 1. 解析模块工作目录
    module_dir = Path(working_dir)
    if not module_dir.is_absolute():
        module_dir = (project_root / module_dir).resolve()
    else:
        module_dir = module_dir.resolve()

    # 2. 解析可执行文件路径
    exe_path = Path(executable)
    if not exe_path.is_absolute():
        # 如果 executable 写的是 backend/installed_modules/xxx/xxx.exe
        # 就基于项目根目录拼接
        exe_path = (project_root / exe_path).resolve()
    else:
        exe_path = exe_path.resolve()

    if not exe_path.exists():
        raise HTTPException(status_code=400, detail=f"可执行文件不存在: {exe_path}")

    if not module_dir.exists():
        raise HTTPException(status_code=400, detail=f"工作目录不存在: {module_dir}")

    values = dict(inputs)
    values["executable"] = str(exe_path)
    values["working_dir"] = str(module_dir)

    runtime_env = os.environ.copy()

    # 强制优先加载模块自己的依赖目录
    # 先读 module.json/modules.json 里的 dependency_dirs，没有的话默认只加 deps
    dependency_dirs = module.get("dependency_dirs") or ["deps"]

    dll_search_dirs = [str(module_dir)]

    for dep in dependency_dirs:
        dep_path = (module_dir / dep).resolve()
        if dep_path.exists() and dep_path.is_dir():
            dll_search_dirs.append(str(dep_path))

    # 去重，保持顺序
    seen = set()
    ordered_dirs = []
    for p in dll_search_dirs:
        if p not in seen:
            ordered_dirs.append(p)
            seen.add(p)

    runtime_env["PATH"] = ";".join(ordered_dirs + [runtime_env.get("PATH", "")])

    # 避免 OpenBLAS 线程过多导致崩溃
    runtime_env["OPENBLAS_NUM_THREADS"] = "1"
    runtime_env["OMP_NUM_THREADS"] = "1"
    runtime_env["GOTO_NUM_THREADS"] = "1"

    # 便于排查 DLL 搜索路径
    runtime_env["MODULE_DLL_DIRS"] = ";".join(ordered_dirs)

    if config_mode in {"json", "json_file", "config_json"}:
        # 为本次任务创建独立 runtime 目录
        runtime_task_dir = Path(tempfile.mkdtemp(prefix="job_", dir=str(RUNTIME_DIR)))

        config_path = runtime_task_dir / "config.json"
        config_path.write_text(
            json.dumps(inputs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        values["config_json"] = str(config_path)
        values["config_path"] = str(config_path)
        values["runtime_dir"] = str(runtime_task_dir)

        if not command_template:
            command_template = ["{executable}", "{config_json}"]
    else:
        if not command_template:
            command_template = ["{executable}"]

    command = format_command(command_template, values)

    # 强制 cwd 为模块目录
    return command, str(module_dir), runtime_env

def install_uploaded_zip(zip_path: Path) -> dict:
    temp_dir = Path(tempfile.mkdtemp(prefix="module_zip_"))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_dir)

        module_json_candidates = list(temp_dir.rglob("module.json"))
        if not module_json_candidates:
            raise HTTPException(status_code=400, detail="压缩包中未找到 module.json")

        module_json_path = module_json_candidates[0]
        module_root = module_json_path.parent

        module_data = json.loads(module_json_path.read_text(encoding="utf-8"))
        module_id = module_data.get("id")
        if not module_id:
            raise HTTPException(status_code=400, detail="module.json 缺少 id")

        target_dir = INSTALLED_MODULES_DIR / module_id
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(module_root, target_dir)

        executable = module_data.get("executable", "")
        if executable and not Path(executable).is_absolute():
            module_data["executable"] = str((target_dir / executable).resolve())

        working_dir = module_data.get("working_dir", ".")
        if working_dir == ".":
            module_data["working_dir"] = str(target_dir.resolve())
        elif not Path(working_dir).is_absolute():
            module_data["working_dir"] = str((target_dir / working_dir).resolve())

        upsert_module(module_data)
        return module_data
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# =========================
# 认证接口
# =========================
@app.post("/api/auth/login")
def api_login(payload: LoginRequest):
    user = verify_user(payload.username, payload.password, payload.role)
    if not user:
        raise HTTPException(status_code=401, detail="用户名、密码或身份不正确")

    token = create_token(user)
    return {
        "token": token,
        "user": sanitize_user(user),
    }


@app.post("/api/auth/register")
def api_register(payload: RegisterRequest):
    try:
        user = register_user(
            payload.username,
            payload.password,
            payload.security_question,
            payload.security_answer,
        )
        return {"ok": True, "user": sanitize_user(user)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/auth/forgot-password/question")
def api_forgot_password_question(username: str):
    try:
        return {"question": get_security_question(username)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/forgot-password/reset")
def api_forgot_password_reset(payload: ForgotPasswordResetRequest):
    try:
        reset_password_by_security_answer(
            payload.username,
            payload.answer,
            payload.new_password,
        )
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/logout")
def api_logout(authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")

    token = authorization.split(" ", 1)[1].strip()
    remove_token(token)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    return sanitize_user(user)


# =========================
# 用户管理接口
# =========================
@app.get("/api/admin/users")
def api_list_users(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return [sanitize_user(u) for u in load_users()]


@app.post("/api/admin/users")
def api_add_user(payload: AddUserRequest, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    try:
        user = create_user(
            payload.username,
            payload.password,
            payload.role,
            payload.security_question,
            payload.security_answer,
        )
        return {"ok": True, "user": sanitize_user(user)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/admin/users/{username}")
def api_delete_user(username: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    try:
        delete_user(username)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/admin/users/{username}/role")
def api_update_user_role(
    username: str,
    payload: UpdateUserRoleRequest,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    try:
        update_user_role(username, payload.role)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/admin/users/{username}/enabled")
def api_update_user_enabled(
    username: str,
    payload: UpdateUserEnabledRequest,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    try:
        update_user_enabled(username, payload.enabled)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/admin/users/{username}/password")
def api_reset_user_password(
    username: str,
    payload: ResetUserPasswordRequest,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    try:
        admin_reset_password(username, payload.new_password)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =========================
# 模块接口
# =========================
@app.get("/api/modules")
def api_list_modules(authorization: str | None = Header(default=None)):
    get_current_user(authorization)
    modules = [m for m in load_modules() if m.get("enabled", True)]
    return modules


@app.get("/api/admin/modules")
def api_admin_list_modules(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return load_modules()


@app.post("/api/admin/modules")
def api_save_module(payload: ModuleSaveRequest, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    module_data = payload.model_dump()
    upsert_module(module_data)
    return {"ok": True, "module": module_data}


@app.delete("/api/admin/modules/{module_id}")
def api_delete_module(module_id: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    ok = remove_module(module_id)
    if not ok:
        raise HTTPException(status_code=404, detail="模块不存在")
    return {"ok": True}


@app.post("/api/admin/modules/upload")
def api_upload_module_zip(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    suffix = Path(file.filename or "module.zip").suffix or ".zip"
    temp_zip = Path(tempfile.mktemp(suffix=suffix))
    try:
        with temp_zip.open("wb") as f:
            f.write(file.file.read())

        module_data = install_uploaded_zip(temp_zip)
        return {"ok": True, "module": module_data}
    finally:
        if temp_zip.exists():
            temp_zip.unlink(missing_ok=True)


# =========================
# 任务接口
# =========================
@app.get("/api/tasks")
def api_list_tasks(authorization: str | None = Header(default=None)):
    get_current_user(authorization)
    return task_manager.list_tasks()


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: str, authorization: str | None = Header(default=None)):
    get_current_user(authorization)
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.post("/api/tasks/{task_id}/cancel")
def api_cancel_task(task_id: str, authorization: str | None = Header(default=None)):
    get_current_user(authorization)
    ok = task_manager.cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: str, authorization: str | None = Header(default=None)):
    get_current_user(authorization)
    ok = task_manager.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@app.post("/api/tasks/run")
def api_run_module(payload: ModuleRunRequest, authorization: str | None = Header(default=None)):
    get_current_user(authorization)

    module = get_module(payload.module_id)
    if not module:
        raise HTTPException(status_code=404, detail="模块不存在")
    if not module.get("enabled", True):
        raise HTTPException(status_code=400, detail="模块已禁用")

    inputs = payload.inputs or {}

    for field in module.get("inputs", []):
        key = field.get("key")
        required = field.get("required", False)
        if required and (key not in inputs or inputs.get(key) in ("", None)):
            raise HTTPException(status_code=400, detail=f"缺少必填参数: {key}")

    command, working_dir, runtime_env = build_runtime_for_module(module, inputs)

    task = task_manager.submit_module_task(
        module_id=module["id"],
        module_name=module.get("name", module["id"]),
        command=command,
        inputs=inputs,
        working_dir=working_dir,
        env=runtime_env,
    )
    return task


# =========================
# 本地文件对话框接口
# =========================
def _safe_tk_root():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


@app.post("/api/local/file")
def api_choose_local_file(authorization: str | None = Header(default=None)):
    get_current_user(authorization)
    try:
        from tkinter import filedialog

        root = _safe_tk_root()
        path = filedialog.askopenfilename()
        root.destroy()
        return {"path": path or ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"选择文件失败: {e}")


@app.post("/api/local/dir")
def api_choose_local_dir(authorization: str | None = Header(default=None)):
    get_current_user(authorization)
    try:
        from tkinter import filedialog

        root = _safe_tk_root()
        path = filedialog.askdirectory()
        root.destroy()
        return {"path": path or ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"选择文件夹失败: {e}")


@app.post("/api/local/save-file")
def api_choose_save_file(authorization: str | None = Header(default=None)):
    get_current_user(authorization)
    try:
        from tkinter import filedialog

        root = _safe_tk_root()
        path = filedialog.asksaveasfilename(defaultextension=".tif")
        root.destroy()
        return {"path": path or ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"选择保存文件失败: {e}")


# =========================
# 前端静态文件
# =========================
if FRONTEND_DIST_DIR.exists():
    assets_dir = FRONTEND_DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/favicon.ico")
    def favicon():
        ico = FRONTEND_DIST_DIR / "favicon.ico"
        if ico.exists():
            return FileResponse(str(ico))
        raise HTTPException(status_code=404, detail="favicon.ico not found")

    @app.get("/")
    def index():
        index_file = FRONTEND_DIST_DIR / "index.html"
        if not index_file.exists():
            raise HTTPException(status_code=404, detail="前端未构建")
        return FileResponse(str(index_file))

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        candidate = FRONTEND_DIST_DIR / full_path
        if candidate.exists() and candidate.is_file():
            return FileResponse(str(candidate))

        index_file = FRONTEND_DIST_DIR / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        raise HTTPException(status_code=404, detail="前端未构建")