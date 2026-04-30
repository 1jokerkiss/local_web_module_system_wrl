from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from string import Formatter
from typing import Any, Dict, List, Optional
from datetime import datetime
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
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
TOOLBARS_FILE = DATA_DIR / "toolbars.json"

INSTALLED_MODULES_DIR = BASE_DIR / "installed_modules"
INSTALLED_MODULES_DIR.mkdir(parents=True, exist_ok=True)

# 单机本地投放目录：管理员可以把模块 zip 直接放到这里，前端点“扫描本地目录安装”即可。
MODULE_DROP_DIR = BASE_DIR.parent / "module_drop"
MODULE_DROP_DIR.mkdir(parents=True, exist_ok=True)

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


class ToolBarSaveRequest(BaseModel):
    key: str = ""
    label: str


class ToolBarUpdateRequest(BaseModel):
    key: str = ""
    label: str


class ModuleRunRequest(BaseModel):
    module_id: str
    inputs: Dict[str, Any] = {}
    parallel_workers: int = 1
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
    tool_type: str = "cloud"
    parallel_mode: str = "auto"
    parallel_input_key: str = ""
    parallel_output_key: str = ""
    parallel_file_patterns: str = "*.tif;*.tiff;*.nc;*.hdf;*.h5"
    parallel_output_suffix: str = ".tif"
    enabled: bool = True


class InstallLocalDropRequest(BaseModel):
    tool_type: str = "cloud"
    filename: str = ""
# 通用辅助函数
# 初始默认工具栏。现在云反演 / 气溶胶反演也按普通动态工具栏处理，
# 只在第一次创建 toolbars.json 时写入；之后不会强制重新合并回来。
DEFAULT_TOOLBARS = [
    {"key": "cloud", "label": "云反演", "system": False},
    {"key": "aerosol", "label": "气溶胶反演", "system": False},
]


def normalize_tool_key(value: str) -> str:
    """把工具栏 key 规范化，允许中文名称，但过滤路径和分隔符。"""
    value = (value or "").strip()
    if not value:
        return ""
    value = value.replace("..", "_").replace("/", "_").replace("\\", "_")
    value = "_".join(value.split())
    return value


def make_toolbar_key(label: str) -> str:
    raw = normalize_tool_key(label)
    if raw:
        return raw
    return f"tool_{datetime.now().strftime('%Y%m%d%H%M%S')}"


def ensure_toolbars_file():
    if not TOOLBARS_FILE.exists():
        TOOLBARS_FILE.write_text(
            json.dumps(DEFAULT_TOOLBARS, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_toolbars() -> List[dict]:
    ensure_toolbars_file()
    try:
        raw = json.loads(TOOLBARS_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raw = list(DEFAULT_TOOLBARS)
    except Exception:
        raw = list(DEFAULT_TOOLBARS)

    # 不再把 DEFAULT_TOOLBARS 每次强制合并进来。
    # 这样 cloud / aerosol 删除后不会自动复活，真正变成动态工具栏。
    merged: Dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = normalize_tool_key(str(item.get("key", "")))
        label = str(item.get("label") or key).strip()
        if not key or not label:
            continue
        merged[key] = {
            "key": key,
            "label": label,
            "system": False,
        }

    result = list(merged.values())
    result.sort(key=lambda x: (0 if x.get("key") in {"cloud", "aerosol"} else 1, x.get("label", "")))
    return result

def save_toolbars(toolbars: List[dict]):
    cleaned: List[dict] = []
    seen = set()
    for item in toolbars:
        if not isinstance(item, dict):
            continue
        key = normalize_tool_key(str(item.get("key", "")))
        label = str(item.get("label") or key).strip()
        if not key or not label or key in seen:
            continue
        seen.add(key)
        # 所有工具栏都按动态工具栏保存，不再写 system=True。
        cleaned.append({"key": key, "label": label, "system": False})

    TOOLBARS_FILE.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def add_toolbar(key: str, label: str) -> dict:
    label = (label or "").strip()
    if not label:
        raise ValueError("工具类型名称不能为空")

    key = normalize_tool_key(key) or make_toolbar_key(label)
    toolbars = load_toolbars()
    if any(t.get("key") == key for t in toolbars):
        raise ValueError("工具类型已存在")

    item = {"key": key, "label": label, "system": False}
    toolbars.append(item)
    save_toolbars(toolbars)
    return item


def update_toolbar(old_key: str, new_key: str, label: str) -> dict:
    old_key = normalize_tool_key(old_key)
    if not old_key:
        raise ValueError("工具类型标识不能为空")

    label = (label or "").strip()
    if not label:
        raise ValueError("工具类型名称不能为空")

    toolbars = load_toolbars()
    found = None
    for item in toolbars:
        if item.get("key") == old_key:
            found = item
            break

    # 有些历史模块可能只有 tool_type，没有在 toolbars.json 中登记；
    # 编辑时也允许把这个虚拟工具栏补登记后更新。
    if not found:
        modules = load_modules()
        if any(module.get("tool_type") == old_key for module in modules):
            found = {"key": old_key, "label": old_key, "system": False}
            toolbars.append(found)
        else:
            raise ValueError("工具栏不存在")

    candidate_key = normalize_tool_key(new_key) or old_key

    if candidate_key != old_key and any(t.get("key") == candidate_key for t in toolbars):
        raise ValueError("新的工具类型标识已存在")

    updated = {
        "key": candidate_key,
        "label": label,
        "system": False,
    }

    new_toolbars = []
    replaced = False
    for item in toolbars:
        if item.get("key") == old_key and not replaced:
            new_toolbars.append(updated)
            replaced = True
        elif item.get("key") != old_key:
            new_toolbars.append(item)

    if not replaced:
        new_toolbars.append(updated)

    save_toolbars(new_toolbars)

    # 修改 key 时，同步迁移该工具栏下模块的 tool_type。
    if candidate_key != old_key:
        modules = load_modules()
        changed = False
        for module in modules:
            if module.get("tool_type") == old_key:
                module["tool_type"] = candidate_key
                changed = True
        if changed:
            save_modules(modules)

    return updated

def delete_toolbar(key: str) -> dict:
    key = normalize_tool_key(key)
    if not key:
        raise ValueError("工具类型标识不能为空")

    toolbars = load_toolbars()
    exists_in_toolbar_file = any(item.get("key") == key for item in toolbars)

    modules = load_modules()
    affected_modules = [module for module in modules if module.get("tool_type") == key]

    if not exists_in_toolbar_file and not affected_modules:
        raise ValueError("工具栏不存在")

    remaining_toolbars = [item for item in toolbars if item.get("key") != key]

    moved_count = 0
    target_tool_type = ""
    if affected_modules:
        # 删除有模块的工具栏时，不删除模块；自动移动到其它工具栏。
        # 如果没有其它工具栏，则自动创建“未分类”。
        if remaining_toolbars:
            target_tool_type = remaining_toolbars[0].get("key") or "uncategorized"
        else:
            target_tool_type = "uncategorized"
            remaining_toolbars.append({"key": target_tool_type, "label": "未分类", "system": False})

        for module in modules:
            if module.get("tool_type") == key:
                module["tool_type"] = target_tool_type
                moved_count += 1

        save_modules(modules)

    save_toolbars(remaining_toolbars)
    return {
        "deleted_key": key,
        "moved_count": moved_count,
        "target_tool_type": target_tool_type,
    }

def ensure_toolbar_exists(key: str, label: str | None = None):
    key = normalize_tool_key(key) or "cloud"
    toolbars = load_toolbars()
    if any(t.get("key") == key for t in toolbars):
        return
    toolbars.append({"key": key, "label": label or key, "system": False})
    save_toolbars(toolbars)


def guess_module_tool_type(module: dict) -> str:
    explicit = normalize_tool_key(str(module.get("tool_type") or module.get("category") or ""))
    if explicit:
        return explicit

    text = " ".join(
        str(x or "")
        for x in [
            module.get("id"),
            module.get("name"),
            module.get("description"),
            " ".join(module.get("tags") or []),
        ]
    ).lower()

    if any(k in text for k in ["aod", "aerosol", "气溶胶", "h8", "polar", "偏振"]):
        return "aerosol"
    if any(k in text for k in ["cloud", "云", "cloud_type", "cth"]):
        return "cloud"
    return "cloud"


def normalize_module_record(module: dict) -> dict:
    if not isinstance(module, dict):
        return {}
    copied = dict(module)
    copied["tool_type"] = guess_module_tool_type(copied)
    copied["parallel_mode"] = str(copied.get("parallel_mode") or "auto")
    copied["parallel_input_key"] = str(copied.get("parallel_input_key") or "")
    copied["parallel_output_key"] = str(copied.get("parallel_output_key") or "")
    copied["parallel_file_patterns"] = str(
        copied.get("parallel_file_patterns") or "*.tif;*.tiff;*.nc;*.hdf;*.h5"
    )
    copied["parallel_output_suffix"] = str(copied.get("parallel_output_suffix") or ".tif")
    return copied


def ensure_modules_file():
    if not MODULES_FILE.exists():
        MODULES_FILE.write_text("[]", encoding="utf-8")
def load_modules() -> List[dict]:
    ensure_modules_file()
    try:
        data = json.loads(MODULES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [normalize_module_record(item) for item in data if isinstance(item, dict)]
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
    user_dir = UPLOADS_DIR / user.username
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
    user_dir = UPLOADS_DIR / user.username
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
    user_dir = UPLOADS_DIR / user.username
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
    user_dir = UPLOADS_DIR / user.username
    user_dir.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(filename)
    target = user_dir / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(str(target), filename=target.name)

def upsert_module(module_data: dict):
    module_data = normalize_module_record(module_data)
    ensure_toolbar_exists(module_data.get("tool_type") or "cloud")
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

def install_uploaded_zip(zip_path: Path, tool_type: str | None = None) -> dict:
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
        selected_tool_type = normalize_tool_key(tool_type or module_data.get("tool_type") or "") or guess_module_tool_type(module_data)
        module_data["tool_type"] = selected_tool_type
        ensure_toolbar_exists(selected_tool_type)

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
# 工具栏 / 工具类型接口
# =========================
@app.get("/api/toolbars")
def api_list_toolbars(authorization: str | None = Header(default=None)):
    get_current_user(authorization)
    return load_toolbars()


@app.get("/api/admin/toolbars")
def api_admin_list_toolbars(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return load_toolbars()


@app.post("/api/admin/toolbars")
def api_add_toolbar(payload: ToolBarSaveRequest, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    try:
        item = add_toolbar(payload.key, payload.label)
        return {"ok": True, "toolbar": item}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/admin/toolbars/{toolbar_key}")
def api_update_toolbar(
    toolbar_key: str,
    payload: ToolBarUpdateRequest,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    try:
        item = update_toolbar(toolbar_key, payload.key, payload.label)
        return {"ok": True, "toolbar": item}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/admin/toolbars/{toolbar_key}")
def api_delete_toolbar(toolbar_key: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    try:
        result = delete_toolbar(toolbar_key)
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# 有些本地打包/代理环境对 DELETE 支持不好，前端删除工具栏统一走这个 POST 接口。
@app.post("/api/admin/toolbars/{toolbar_key}/delete")
def api_delete_toolbar_post(toolbar_key: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    try:
        result = delete_toolbar(toolbar_key)
        return {"ok": True, **result}
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
    tool_type: str = Form("cloud"),
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    selected_tool_type = normalize_tool_key(tool_type) or "cloud"
    ensure_toolbar_exists(selected_tool_type)

    suffix = Path(file.filename or "module.zip").suffix or ".zip"
    temp_zip = Path(tempfile.mktemp(suffix=suffix))
    try:
        with temp_zip.open("wb") as f:
            f.write(file.file.read())

        module_data = install_uploaded_zip(temp_zip, selected_tool_type)
        return {"ok": True, "module": module_data}
    finally:
        if temp_zip.exists():
            temp_zip.unlink(missing_ok=True)


@app.get("/api/admin/modules/drop-zips")
def api_list_module_drop_zips(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    MODULE_DROP_DIR.mkdir(parents=True, exist_ok=True)
    zips = []
    for p in sorted(MODULE_DROP_DIR.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
        stat = p.stat()
        zips.append({
            "name": p.name,
            "path": str(p.resolve()),
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    return {"drop_dir": str(MODULE_DROP_DIR.resolve()), "items": zips}


def archive_installed_zip(zip_path: Path):
    archive_dir = MODULE_DROP_DIR / "installed"
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / zip_path.name
    if target.exists():
        target = archive_dir / f"{zip_path.stem}_{datetime.now().strftime('%Y%m%d%H%M%S')}{zip_path.suffix}"
    shutil.move(str(zip_path), str(target))


@app.post("/api/admin/modules/install-local-drop")
def api_install_modules_from_local_drop(
    payload: InstallLocalDropRequest,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    MODULE_DROP_DIR.mkdir(parents=True, exist_ok=True)

    selected_tool_type = normalize_tool_key(payload.tool_type) or "cloud"
    ensure_toolbar_exists(selected_tool_type)

    if payload.filename:
        safe_name = sanitize_filename(payload.filename)
        candidates = [MODULE_DROP_DIR / safe_name]
    else:
        candidates = sorted(MODULE_DROP_DIR.glob("*.zip"), key=lambda x: x.stat().st_mtime)

    installed = []
    failed = []
    for zip_path in candidates:
        if not zip_path.exists() or not zip_path.is_file() or zip_path.suffix.lower() != ".zip":
            failed.append({"name": zip_path.name, "error": "zip 文件不存在"})
            continue
        try:
            module_data = install_uploaded_zip(zip_path, selected_tool_type)
            installed.append(module_data)
            archive_installed_zip(zip_path)
        except Exception as e:
            failed.append({"name": zip_path.name, "error": str(e)})

    return {
        "ok": len(failed) == 0,
        "drop_dir": str(MODULE_DROP_DIR.resolve()),
        "installed": installed,
        "failed": failed,
    }



# =========================
# 并行执行辅助逻辑
# =========================
VALID_PARALLEL_MODES = {"none", "auto", "single_file", "folder_chunks", "module_internal"}
DEFAULT_PARALLEL_PATTERNS = "*.tif;*.tiff;*.nc;*.hdf;*.h5"


def clamp_parallel_workers(value: int | str | None) -> int:
    try:
        n = int(value or 1)
    except Exception:
        n = 1
    return max(1, min(n, 64))


def parse_parallel_patterns(pattern_text: str | None) -> list[str]:
    raw = str(pattern_text or DEFAULT_PARALLEL_PATTERNS)
    parts = []
    for item in raw.replace(",", ";").split(";"):
        item = item.strip()
        if item:
            parts.append(item)
    return parts or ["*"]


def field_meta(module: dict, key: str) -> dict:
    for item in module.get("inputs", []) or []:
        if item.get("key") == key:
            return item
    return {}


def choose_parallel_input_key(module: dict, inputs: dict) -> str:
    explicit = str(module.get("parallel_input_key") or "").strip()
    if explicit:
        return explicit

    input_fields = module.get("inputs", []) or []
    for field in input_fields:
        key = field.get("key")
        if key in inputs and field.get("type") in {"file_path", "dir_path"}:
            return key

    preferred_words = ["input", "infile", "file", "inpath", "folder", "dir", "path"]
    for word in preferred_words:
        for key, value in inputs.items():
            if word in str(key).lower() and value not in ("", None):
                return key

    for key, value in inputs.items():
        if value not in ("", None):
            return key

    return ""


def discover_batch_files(path_value: str, patterns: list[str]) -> list[Path]:
    root = Path(path_value).expanduser()
    if root.is_file():
        return [root.resolve()]
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=400, detail=f"并行输入路径不存在或不是文件夹: {root}")

    found: list[Path] = []
    seen = set()
    for pattern in patterns:
        for item in root.rglob(pattern):
            if item.is_file():
                rp = item.resolve()
                if rp not in seen:
                    seen.add(rp)
                    found.append(rp)
    found.sort(key=lambda x: str(x).lower())
    return found


def split_evenly(items: list[Path], parts: int) -> list[list[Path]]:
    parts = max(1, min(parts, len(items)))
    buckets = [[] for _ in range(parts)]
    for idx, item in enumerate(items):
        buckets[idx % parts].append(item)
    return [bucket for bucket in buckets if bucket]


def link_or_copy_file(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except Exception:
        try:
            if hasattr(os, "symlink"):
                os.symlink(src, dst)
            else:
                shutil.copy2(src, dst)
        except Exception:
            shutil.copy2(src, dst)


def unique_chunk_filename(src: Path, used: set[str]) -> str:
    name = src.name
    if name not in used:
        used.add(name)
        return name
    stem, suffix = src.stem, src.suffix
    idx = 1
    while True:
        candidate = f"{stem}_{idx}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        idx += 1


def is_probably_dir_output(module: dict, output_key: str, output_value: str) -> bool:
    meta = field_meta(module, output_key)
    k = output_key.lower()
    label = str(meta.get("label") or "").lower()
    if meta.get("type") == "dir_path":
        return True
    if "dir" in k or "folder" in k or "目录" in label or "文件夹" in label:
        return True
    p = Path(output_value)
    return bool(output_value) and (p.exists() and p.is_dir())


def apply_single_file_output_mapping(module: dict, base_inputs: dict, input_file: Path) -> dict:
    new_inputs = dict(base_inputs)
    output_key = str(module.get("parallel_output_key") or "").strip()
    if not output_key or output_key not in new_inputs:
        return new_inputs

    output_value = str(new_inputs.get(output_key) or "").strip()
    if not output_value:
        return new_inputs

    if is_probably_dir_output(module, output_key, output_value):
        # 输出字段本身就是目录时，不改字段值，让模块自己在目录里生成结果。
        Path(output_value).mkdir(parents=True, exist_ok=True)
        return new_inputs

    out_path = Path(output_value)
    suffix = str(module.get("parallel_output_suffix") or ".tif")
    if not suffix.startswith("."):
        suffix = "." + suffix

    if out_path.suffix:
        mapped = out_path.with_name(f"{out_path.stem}_{input_file.stem}{out_path.suffix}")
    else:
        out_path.mkdir(parents=True, exist_ok=True)
        mapped = out_path / f"{input_file.stem}{suffix}"

    mapped.parent.mkdir(parents=True, exist_ok=True)
    new_inputs[output_key] = str(mapped.resolve())
    return new_inputs


def infer_parallel_mode(module: dict, inputs: dict, input_key: str) -> str:
    mode = str(module.get("parallel_mode") or "auto").strip() or "auto"
    if mode not in VALID_PARALLEL_MODES:
        mode = "auto"
    if mode != "auto":
        return mode

    value = inputs.get(input_key)
    if not value:
        return "none"
    p = Path(str(value))
    meta = field_meta(module, input_key)
    if p.is_file():
        return "single_file"
    if meta.get("type") == "file_path":
        return "single_file"
    return "folder_chunks"


def prepare_parallel_jobs(module: dict, inputs: dict, parallel_workers: int) -> list[dict]:
    workers = clamp_parallel_workers(parallel_workers)
    if workers <= 1:
        return []

    input_key = choose_parallel_input_key(module, inputs)
    mode = infer_parallel_mode(module, inputs, input_key) if input_key else "none"

    if mode == "none":
        return []

    if mode == "module_internal":
        # 该模式不拆任务，只在 api_run_module 中把 parallel_workers 传给模块。
        return []

    if not input_key:
        raise HTTPException(status_code=400, detail="未找到并行输入字段，请在模块配置中填写 parallel_input_key")

    input_value = inputs.get(input_key)
    if input_value in ("", None):
        raise HTTPException(status_code=400, detail=f"并行输入字段为空: {input_key}")

    patterns = parse_parallel_patterns(module.get("parallel_file_patterns"))
    files = discover_batch_files(str(input_value), patterns)
    if not files:
        raise HTTPException(status_code=400, detail=f"未匹配到可并行处理的文件，匹配规则: {';'.join(patterns)}")

    jobs: list[dict] = []
    if mode == "single_file":
        for idx, file_path in enumerate(files, start=1):
            job_inputs = apply_single_file_output_mapping(module, inputs, file_path)
            job_inputs[input_key] = str(file_path)
            job_inputs["_parallel_workers"] = 1
            job_inputs["_parallel_index"] = idx
            job_inputs["_parallel_total"] = len(files)
            command, working_dir, runtime_env = build_runtime_for_module(module, job_inputs)
            jobs.append({
                "module_id": module.get("id", ""),
                "module_name": module.get("name", module.get("id", "")),
                "label": file_path.name,
                "command": command,
                "working_dir": working_dir,
                "env": runtime_env,
                "inputs": job_inputs,
            })
        return jobs

    if mode == "folder_chunks":
        if Path(str(input_value)).is_file():
            # 传入单个文件时退化为 single_file。
            return prepare_parallel_jobs({**module, "parallel_mode": "single_file"}, inputs, workers)

        chunks = split_evenly(files, workers)
        chunk_root = RUNTIME_DIR / "parallel_chunks" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        chunk_root.mkdir(parents=True, exist_ok=True)

        for idx, chunk in enumerate(chunks, start=1):
            chunk_dir = chunk_root / f"worker_{idx:02d}"
            chunk_dir.mkdir(parents=True, exist_ok=True)
            used_names: set[str] = set()
            for src in chunk:
                dst = chunk_dir / unique_chunk_filename(src, used_names)
                link_or_copy_file(src, dst)

            job_inputs = dict(inputs)
            job_inputs[input_key] = str(chunk_dir.resolve())
            job_inputs["_parallel_workers"] = 1
            job_inputs["_parallel_index"] = idx
            job_inputs["_parallel_total"] = len(chunks)
            job_inputs["_parallel_chunk_file_count"] = len(chunk)
            command, working_dir, runtime_env = build_runtime_for_module(module, job_inputs)
            jobs.append({
                "module_id": module.get("id", ""),
                "module_name": module.get("name", module.get("id", "")),
                "label": f"worker_{idx:02d} ({len(chunk)} files)",
                "command": command,
                "working_dir": working_dir,
                "env": runtime_env,
                "inputs": job_inputs,
            })
        return jobs

    raise HTTPException(status_code=400, detail=f"不支持的并行模式: {mode}")

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
    parallel_workers = clamp_parallel_workers(payload.parallel_workers)

    for field in module.get("inputs", []):
        key = field.get("key")
        required = field.get("required", False)
        if required and (key not in inputs or inputs.get(key) in ("", None)):
            raise HTTPException(status_code=400, detail=f"缺少必填参数: {key}")

    mode = str(module.get("parallel_mode") or "auto").strip() or "auto"
    if parallel_workers > 1 and mode == "module_internal":
        # 模块源码自己处理并行，平台只负责传参，不拆成多个进程。
        inputs = dict(inputs)
        inputs["parallel_workers"] = parallel_workers
        inputs["_parallel_workers"] = parallel_workers

    jobs = prepare_parallel_jobs(module, inputs, parallel_workers)
    if parallel_workers > 1 and len(jobs) > 1:
        return task_manager.submit_parallel_module_task(
            module_id=module["id"],
            module_name=module.get("name", module["id"]),
            jobs=jobs,
            inputs={**inputs, "parallel_workers": parallel_workers},
            max_workers=parallel_workers,
        )

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