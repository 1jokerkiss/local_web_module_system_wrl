from __future__ import annotations
import stat
import time
import sys
import base64
import json
import io
import os
import re
import shutil
import subprocess
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
from pydantic import BaseModel, ConfigDict

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
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
MODULES_FILE = DATA_DIR / "modules.json"
TASKS_FILE = DATA_DIR / "tasks.json"
TOOLBARS_FILE = DATA_DIR / "toolbars.json"
DATA_FILES_FILE = DATA_DIR / "data_files.json"

INSTALLED_MODULES_DIR = BASE_DIR / "installed_modules"
INSTALLED_MODULES_DIR.mkdir(parents=True, exist_ok=True)
PYTHON_WHEELS_DIR = BASE_DIR / "python_wheels"
PYTHON_WHEELS_DIR.mkdir(parents=True, exist_ok=True)

STRICT_LOCAL_BINARY_PACKAGES = {
    "gdal",
    "rasterio",
    "pyproj",
    "cartopy",
}

PREFER_LOCAL_BINARY_PACKAGES = {
    "numpy",
    "h5py",
}
# Python 源码模块的独立运行环境目录。
# 方案二：不再把 Python 源码打包成 exe，而是为每个 Python 模块创建独立 venv，
# 运行时使用该 venv 的 python.exe 执行入口脚本，并传入平台生成的 config.json。
PYTHON_MODULE_ENVS_DIR = BASE_DIR / "module_envs"
PYTHON_MODULE_ENVS_DIR.mkdir(parents=True, exist_ok=True)

# 单机本地投放目录：管理员可以把模块 zip 直接放到这里，前端点“扫描本地目录安装”即可。
MODULE_DROP_DIR = PROJECT_ROOT / "module_drop"
MODULE_DROP_DIR.mkdir(parents=True, exist_ok=True)

RUNTIME_DIR = BASE_DIR / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"

app = FastAPI(title="云和气溶胶反演系统 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

task_manager = TaskManager(TASKS_FILE)


@app.get("/api/system/resources")
def api_system_resources(authorization: str | None = Header(default=None)):
    """返回本机 CPU 核数、建议进程数、上限进程数和当前任务资源占用。"""
    get_current_user(authorization)
    return task_manager.get_system_resource_info()


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
    model_config = ConfigDict(extra="allow")

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
    parallel: Dict[str, Any] = {}
    enabled: bool = True


class InstallLocalDropRequest(BaseModel):
    tool_type: str = "cloud"
    filename: str = ""


class FilePreviewRequest(BaseModel):
    path: str


class ParseParamJsonRequest(BaseModel):
    path: str
class InstallModuleFolderRequest(BaseModel):
    folder_path: str
    tool_type: str = "cloud"

class PythonFolderModuleUploadRequest(BaseModel):
    source_dir: str
    param_json_path: str
    module_id: str
    module_name: str
    entry_file: str = "main.py"
    tool_type: str = ""
    description: str = ""

class PythonModuleConfigRequest(BaseModel):
    path: str
# 通用辅助函数
VALID_PARALLEL_MODES = {"none", "auto", "single_file", "folder_chunks", "module_internal"}
DEFAULT_PARALLEL_PATTERNS = "*.tif;*.tiff;*.nc;*.hdf;*.h5"

# 初始默认工具栏。现在云反演 / 气溶胶反演也按普通动态工具栏处理，
# 只在第一次创建 toolbars.json 时写入；之后不会强制重新合并回来。
DEFAULT_TOOLBARS = [
    {"key": "cloud", "label": "云反演", "system": False},
    {"key": "aerosol", "label": "气溶胶反演", "system": False},
]

def to_project_relative_path(path: Path) -> str:
    """
    项目内部路径保存为相对于项目根目录的路径。
    例如：
    D:/xxx/local_web/backend/installed_modules/cth/main.exe
    保存成：
    backend/installed_modules/cth/main.exe

    如果路径不在项目目录内部，则保留绝对路径。
    """
    resolved = path.resolve()

    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def resolve_packaged_module_path(raw_value: str, module_id: str, target_dir: Path, default_path: Path) -> Path:
    """
    把 module.json 里的 executable / working_dir 转成安装后的真实路径。
    优先支持相对路径；如果 module.json 里误写了旧电脑绝对路径，则尽量兜底处理。
    """
    raw_value = str(raw_value or "").strip()

    if not raw_value or raw_value == ".":
        return default_path

    p = Path(raw_value)

    # 正常情况：module.json 里写的是相对路径
    if not p.is_absolute():
        return target_dir / p

    # 兜底情况：module.json 里写了绝对路径
    # 如果路径里包含模块 id，例如 .../installed_modules/cth/xxx.exe
    # 则取模块 id 后面的部分。
    parts = list(p.parts)
    if module_id in parts:
        idx = parts.index(module_id)
        rel_parts = parts[idx + 1:]
        if rel_parts:
            return target_dir.joinpath(*rel_parts)

    # executable 是绝对路径时，最后兜底用文件名
    if p.suffix:
        return target_dir / p.name

    # working_dir 是绝对路径时，最后兜底用模块根目录
    return default_path
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


def normalize_parallel_config(module: dict) -> dict:
    raw = module.get("parallel")
    if not isinstance(raw, dict):
        raw = {}
    cfg = {
        "mode": raw.get("mode") or module.get("parallel_mode") or "auto",
        "input_key": raw.get("input_key") or module.get("parallel_input_key") or "",
        "output_key": raw.get("output_key") or module.get("parallel_output_key") or "",
        "file_patterns": raw.get("file_patterns") or module.get("parallel_file_patterns") or "*.tif;*.tiff;*.nc;*.hdf;*.h5",
        "output_suffix": raw.get("output_suffix") or module.get("parallel_output_suffix") or ".tif",
    }
    mode = str(cfg.get("mode") or "auto").strip() or "auto"
    cfg["mode"] = mode if mode in VALID_PARALLEL_MODES else "auto"
    cfg["input_key"] = str(cfg.get("input_key") or "")
    cfg["output_key"] = str(cfg.get("output_key") or "")
    cfg["file_patterns"] = str(cfg.get("file_patterns") or "*.tif;*.tiff;*.nc;*.hdf;*.h5")
    cfg["output_suffix"] = str(cfg.get("output_suffix") or ".tif")
    return cfg


def normalize_module_record(module: dict) -> dict:
    if not isinstance(module, dict):
        return {}
    copied = dict(module)
    copied["tool_type"] = guess_module_tool_type(copied)
    copied["parallel"] = normalize_parallel_config(copied)
    # 旧版本的并行平铺字段不再写回，避免管理页面变乱。
    for key in [
        "parallel_mode",
        "parallel_input_key",
        "parallel_output_key",
        "parallel_file_patterns",
        "parallel_output_suffix",
    ]:
        copied.pop(key, None)
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


def is_tif_path(path: Path) -> bool:
    return path.suffix.lower() in {".tif", ".tiff"}


def _preview_valid_mask(array, nodata=None, prefer_nonzero: bool = True):
    """生成预览用的有效像元掩膜。

    很多遥感产品会把背景、无效区域写成 0、-9999、65535 等值。
    如果直接用全图 2%-98% 分位拉伸，背景 0 占比过高时整张预览会被压成黑色。
    """
    import numpy as np

    arr = array.astype("float32", copy=False)
    finite = np.isfinite(arr)
    mask = finite.copy()

    if nodata is not None:
        try:
            nd = float(nodata)
            if np.isfinite(nd):
                mask &= ~np.isclose(arr, nd, rtol=0, atol=1e-6)
        except Exception:
            pass

    for fill_value in (-999999.0, -99999.0, -9999.0, -999.0, -32768.0, 32767.0, 65535.0):
        mask &= ~np.isclose(arr, fill_value, rtol=0, atol=1e-6)

    if not mask.any():
        return mask

    if prefer_nonzero:
        valid = arr[mask]
        zero_ratio = float(np.mean(np.isclose(valid, 0.0, rtol=0, atol=1e-12))) if valid.size else 0.0
        nonzero_mask = mask & ~np.isclose(arr, 0.0, rtol=0, atol=1e-12)
        if zero_ratio >= 0.50 and nonzero_mask.any():
            return nonzero_mask

    return mask


def _array_preview_stats(array, nodata=None) -> dict:
    import numpy as np

    arr = array.astype("float32", copy=False)
    finite = np.isfinite(arr)
    base_mask = finite.copy()

    if nodata is not None:
        try:
            nd = float(nodata)
            if np.isfinite(nd):
                base_mask &= ~np.isclose(arr, nd, rtol=0, atol=1e-6)
        except Exception:
            pass

    stretch_mask = _preview_valid_mask(arr, nodata=nodata, prefer_nonzero=True)
    stats: dict[str, Any] = {
        "shape": list(arr.shape),
        "nodata": nodata,
        "finite_pixels": int(finite.sum()),
        "valid_pixels": int(base_mask.sum()),
        "stretch_pixels": int(stretch_mask.sum()),
    }

    if base_mask.any():
        vals = arr[base_mask]
        stats.update({
            "min": float(np.nanmin(vals)),
            "max": float(np.nanmax(vals)),
            "mean": float(np.nanmean(vals)),
            "p2": float(np.nanpercentile(vals, 2)),
            "p98": float(np.nanpercentile(vals, 98)),
            "zero_ratio": float(np.mean(np.isclose(vals, 0.0, rtol=0, atol=1e-12))),
        })
    if stretch_mask.any():
        vals = arr[stretch_mask]
        stats.update({
            "stretch_min": float(np.nanmin(vals)),
            "stretch_max": float(np.nanmax(vals)),
            "stretch_p2": float(np.nanpercentile(vals, 2)),
            "stretch_p98": float(np.nanpercentile(vals, 98)),
        })
    return stats


def _normalize_to_uint8(array, nodata=None, prefer_nonzero: bool = True):
    import numpy as np

    arr = array.astype("float32", copy=False)
    mask = _preview_valid_mask(arr, nodata=nodata, prefer_nonzero=prefer_nonzero)
    if not mask.any():
        return np.zeros(arr.shape, dtype="uint8")

    valid = arr[mask]
    lo, hi = np.nanpercentile(valid, [2, 98])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(np.nanmin(valid))
        hi = float(np.nanmax(valid))

    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        out = np.zeros(arr.shape, dtype="uint8")
        out[mask] = 180
        return out

    out = np.zeros(arr.shape, dtype="float32")
    out[mask] = (arr[mask] - lo) / (hi - lo) * 255.0
    out = np.clip(out, 0, 255)
    return out.astype("uint8")


def _colorize_gray(gray):
    """单波段数据转为更容易辨识的伪彩色预览。"""
    from PIL import Image

    img = Image.fromarray(gray, mode="L")
    try:
        from PIL import ImageOps
        return ImageOps.colorize(img, black="#000000", mid="#1d4ed8", white="#fff7a8")
    except Exception:
        return img.convert("RGB")


def _clean_cli_text(value: str) -> str:
    return (value or "").strip().replace("\r", " ").replace("\n", " ")

def _find_gdal_command(command_name: str) -> str:
    """查找系统 GDAL 命令。Windows 下 gdalinfo 可用但 Python 没装 osgeo 时会走这里。"""
    candidate = shutil.which(command_name)
    if candidate:
        return candidate
    raise HTTPException(
        status_code=500,
        detail=(
            f"系统未找到 {command_name} 命令。当前 Python 没有 osgeo 模块时，"
            f"需要把 GDAL 命令行工具加入 PATH，或给后端 Python 安装 GDAL/osgeo。"
        ),
    )

def _run_gdal_cli(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"未找到 GDAL 命令: {args[0]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail=f"GDAL 命令执行超时: {' '.join(args)}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GDAL 命令执行失败: {exc}")


def _read_tif_meta_with_gdalinfo_cli(tif_path: Path) -> dict:
    """用 gdalinfo -json 读取基础元数据。失败也不影响预览。"""
    try:
        gdalinfo = _find_gdal_command("gdalinfo")
        result = _run_gdal_cli([gdalinfo, "-json", str(tif_path)], timeout=60)
        if result.returncode != 0:
            return {"gdalinfo_error": _clean_cli_text(result.stderr or result.stdout)}
        data = json.loads(result.stdout or "{}")
        size = data.get("size") or []
        bands = data.get("bands") or []
        meta: dict[str, Any] = {
            "gdalinfo_driver": (data.get("driverShortName") or data.get("driverLongName") or ""),
            "width": int(size[0]) if len(size) > 0 else None,
            "height": int(size[1]) if len(size) > 1 else None,
            "bands": len(bands),
        }
        band_types = []
        nodata_values = []
        for band in bands[:8]:
            if band.get("type"):
                band_types.append(str(band.get("type")))
            if band.get("noDataValue") is not None:
                nodata_values.append(band.get("noDataValue"))
        if band_types:
            meta["band_types"] = band_types
        if nodata_values:
            meta["nodata_values"] = nodata_values
        return meta
    except Exception as exc:
        return {"gdalinfo_error": str(exc)}
def _render_tif_with_gdal_cli(tif_path: Path, meta: dict[str, Any] | None = None) -> dict:
    """
    Python 环境没有 osgeo 时，调用系统 gdal_translate 把 GeoTIFF 转成 PNG。
    这样只要命令行 gdalinfo/gdal_translate 可用，就能预览多波段遥感 TIFF。
    """
    meta = dict(meta or {})
    cli_meta = _read_tif_meta_with_gdalinfo_cli(tif_path)
    meta.update({k: v for k, v in cli_meta.items() if v is not None})

    band_count = int(meta.get("bands") or 1)
    gdal_translate = _find_gdal_command("gdal_translate")

    with tempfile.TemporaryDirectory(prefix="tif_preview_") as tmpdir:
        out_png = Path(tmpdir) / "preview.png"

        cmd = [
            gdal_translate,
            "-q",
            "-of",
            "PNG",
            "-ot",
            "Byte",
            "-scale",
            "-outsize",
            "1600",
            "0",
        ]

        if band_count >= 3:
            # 多波段遥感 TIFF 默认取 1/2/3 波段做 RGB 预览。
            # 后续如需严格真彩色/假彩色，可再在前端加波段选择。
            cmd.extend(["-b", "1", "-b", "2", "-b", "3"]);
            meta["render_mode"] = "gdal_translate_cli_rgb_1_2_3"
        else:
            cmd.extend(["-b", "1"]);
            meta["render_mode"] = "gdal_translate_cli_single_band"

        cmd.extend([str(tif_path), str(out_png)])

        result = _run_gdal_cli(cmd, timeout=120)
        if result.returncode != 0 or not out_png.exists():
            err = _clean_cli_text(result.stderr or result.stdout)
            raise HTTPException(status_code=500, detail=f"gdal_translate 预览失败: {err}")

        meta["preview_engine"] = "gdal_translate_cli"
        meta["gdal_command"] = " ".join(cmd)
        return {"png": out_png.read_bytes(), "meta": meta}
def _png_data_url(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"

def _resize_preview_image(image, max_size: int = 1600):
    """
    限制预览图最大边长，避免大 tif 直接撑爆浏览器。
    """
    try:
        image.thumbnail((max_size, max_size))
    except Exception:
        pass
    return image


def _array_to_preview_png(array, meta: dict | None = None) -> dict:
    """
    把二维/三维数组转成 PNG bytes。
    不依赖 gdal_translate。
    """
    import numpy as np
    from PIL import Image

    meta = dict(meta or {})

    arr = np.asarray(array)
    arr = np.squeeze(arr)

    if arr.ndim == 0:
        raise ValueError("数组没有可预览的二维数据")

    # 如果是多波段，尽量转成 H,W,C
    if arr.ndim == 3:
        # 常见遥感格式：bands, height, width
        if arr.shape[0] <= 8 and arr.shape[1] > 8 and arr.shape[2] > 8:
            if arr.shape[0] >= 3:
                bands = [
                    _normalize_to_uint8(arr[0], nodata=None, prefer_nonzero=True),
                    _normalize_to_uint8(arr[1], nodata=None, prefer_nonzero=True),
                    _normalize_to_uint8(arr[2], nodata=None, prefer_nonzero=True),
                ]
                rgb = np.dstack(bands)
                image = Image.fromarray(rgb, mode="RGB")
                meta["render_mode"] = "python_array_bands_first_rgb"
            else:
                gray = _normalize_to_uint8(arr[0], nodata=None, prefer_nonzero=True)
                image = _colorize_gray(gray)
                meta["render_mode"] = "python_array_bands_first_single"
        # 常见图片格式：height, width, channels
        elif arr.shape[-1] in {3, 4}:
            if arr.shape[-1] == 4:
                arr = arr[:, :, :3]
            bands = [
                _normalize_to_uint8(arr[:, :, 0], nodata=None, prefer_nonzero=True),
                _normalize_to_uint8(arr[:, :, 1], nodata=None, prefer_nonzero=True),
                _normalize_to_uint8(arr[:, :, 2], nodata=None, prefer_nonzero=True),
            ]
            rgb = np.dstack(bands)
            image = Image.fromarray(rgb, mode="RGB")
            meta["render_mode"] = "python_array_channels_last_rgb"
        else:
            # 兜底：取第一个切片
            gray = _normalize_to_uint8(arr[0], nodata=None, prefer_nonzero=True)
            image = _colorize_gray(gray)
            meta["render_mode"] = "python_array_first_slice"
    elif arr.ndim == 2:
        gray = _normalize_to_uint8(arr, nodata=None, prefer_nonzero=True)
        image = _colorize_gray(gray)
        meta.update(_array_preview_stats(arr, nodata=None))
        meta["render_mode"] = "python_array_single_band"
    else:
        raise ValueError(f"暂不支持 {arr.ndim} 维数组预览")

    image = _resize_preview_image(image)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    meta["preview_engine"] = meta.get("preview_engine") or "python_array"
    meta["preview_width"], meta["preview_height"] = image.size
    return {"png": buf.getvalue(), "meta": meta}


def render_tif_to_preview_result(tif_path: Path) -> dict:
    """
    后台 tif 预览：
    1. 优先用 Python osgeo.gdal；
    2. 没有 osgeo 时，尝试 tifffile；
    3. 再尝试 Pillow；
    4. 最后才尝试系统 gdal_translate。
    """
    if not tif_path.exists() or not tif_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    if tif_path.suffix.lower() not in {".tif", ".tiff"}:
        raise HTTPException(status_code=400, detail="只支持预览 tif/tiff 文件")

    meta: dict[str, Any] = {
        "name": tif_path.name,
        "size": tif_path.stat().st_size,
        "suffix": tif_path.suffix.lower(),
    }

    # 方案一：Python osgeo.gdal
    try:
        import numpy as np
        from osgeo import gdal

        ds = gdal.Open(str(tif_path))
        if ds is None:
            raise RuntimeError("GDAL 无法打开该 tif")

        width = int(ds.RasterXSize)
        height = int(ds.RasterYSize)
        band_count = int(ds.RasterCount or 1)

        max_size = 1600
        scale = min(1.0, max_size / max(width, height)) if max(width, height) else 1.0
        out_w = max(1, int(width * scale))
        out_h = max(1, int(height * scale))

        meta.update({
            "width": width,
            "height": height,
            "bands": band_count,
            "preview_engine": "python_osgeo_gdal",
        })

        if band_count >= 3:
            bands = []
            for band_index in (1, 2, 3):
                band = ds.GetRasterBand(band_index)
                nodata = band.GetNoDataValue()
                arr = band.ReadAsArray(buf_xsize=out_w, buf_ysize=out_h)
                bands.append(_normalize_to_uint8(arr, nodata=nodata, prefer_nonzero=True))

            rgb = np.dstack(bands)

            from PIL import Image
            image = Image.fromarray(rgb, mode="RGB")
            meta["render_mode"] = "python_osgeo_rgb"
        else:
            band = ds.GetRasterBand(1)
            nodata = band.GetNoDataValue()
            arr = band.ReadAsArray(buf_xsize=out_w, buf_ysize=out_h)

            from PIL import Image
            gray = _normalize_to_uint8(arr, nodata=nodata, prefer_nonzero=True)
            image = _colorize_gray(gray)
            meta.update(_array_preview_stats(arr, nodata=nodata))
            meta["render_mode"] = "python_osgeo_single_band"

        image = _resize_preview_image(image)

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        meta["preview_width"], meta["preview_height"] = image.size
        return {"png": buf.getvalue(), "meta": meta}

    except Exception as gdal_exc:
        meta["python_osgeo_error"] = str(gdal_exc)

    # 方案二：tifffile
    try:
        import tifffile

        arr = tifffile.imread(str(tif_path))
        meta["preview_engine"] = "python_tifffile"
        try:
            meta["array_shape"] = list(arr.shape)
        except Exception:
            pass

        return _array_to_preview_png(arr, meta)

    except Exception as tifffile_exc:
        meta["tifffile_error"] = str(tifffile_exc)

    # 方案三：Pillow
    try:
        import numpy as np
        from PIL import Image

        image = Image.open(tif_path)

        try:
            arr = np.asarray(image)
            meta["preview_engine"] = "python_pillow_array"
            meta["pillow_mode"] = image.mode
            return _array_to_preview_png(arr, meta)
        except Exception:
            image = Image.open(tif_path)
            image = _resize_preview_image(image)
            if image.mode not in {"L", "RGB", "RGBA"}:
                image = image.convert("RGB")

            buf = io.BytesIO()
            image.save(buf, format="PNG")

            meta["preview_engine"] = "python_pillow"
            meta["pillow_mode"] = image.mode
            meta["preview_width"], meta["preview_height"] = image.size
            return {"png": buf.getvalue(), "meta": meta}

    except Exception as pillow_exc:
        meta["pillow_error"] = str(pillow_exc)

    # 方案四：最后才尝试 gdal_translate
    try:
        return _render_tif_with_gdal_cli(tif_path, meta)
    except Exception as cli_exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "tif 后台预览失败：Python osgeo、tifffile、Pillow、gdal_translate 都无法生成预览。"
                f" osgeo错误: {meta.get('python_osgeo_error')};"
                f" tifffile错误: {meta.get('tifffile_error')};"
                f" Pillow错误: {meta.get('pillow_error')};"
                f" GDAL命令错误: {cli_exc}"
            ),
        )
def get_username_from_user(user) -> str:
    """
    兼容 get_current_user() 返回 dict 或对象两种情况。
    """
    if isinstance(user, dict):
        return str(user.get("username") or "")
    return str(getattr(user, "username", "") or "")
OUTPUT_ROLE_VALUES = {"output", "out", "result", "结果", "输出"}


def data_file_belongs_to_user(item: dict, username: str) -> bool:
    """
    判断 data_files.json 中的一条文件记录是否属于当前用户。
    旧数据没有 owner_username 时默认不显示，避免串用户。
    """
    owner = str(item.get("owner_username") or "").strip()
    return bool(owner) and owner == str(username)


def load_visible_data_files_for_user(username: str) -> tuple[list[dict], list[dict]]:
    """
    返回：
    1. all_items：清理过不存在文件后的全量 data_files
    2. visible_items：当前用户可见的文件列表

    注意：visible_items 的 id 是给前端用的用户内序号；
    _source_index 是它在 all_items 里的真实位置，给 preview/delete/reveal 用。
    """
    all_items = load_data_files()
    kept_items: list[dict] = []
    visible_items: list[dict] = []

    for item in all_items:
        if not isinstance(item, dict):
            continue

        role = str(item.get("io_role") or item.get("data_role") or "output").strip().lower()
        if role not in OUTPUT_ROLE_VALUES:
            continue

        path = Path(str(item.get("path") or ""))
        if not path.exists() or not path.is_file():
            continue

        row = dict(item)

        try:
            stat = path.stat()
            row["size"] = stat.st_size
            row["size_text"] = format_file_size(stat.st_size)
            row["file_name"] = row.get("file_name") or row.get("name") or path.name
            row["name"] = row.get("name") or path.name
            row["io_role"] = "output"
            row["data_role"] = "output"
            row["modified_at"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        except Exception:
            pass

        source_index = len(kept_items)
        kept_items.append(row)

        if data_file_belongs_to_user(row, username):
            visible_row = dict(row)
            visible_row["_source_index"] = source_index
            visible_items.append(visible_row)

    # 全量记录用全局 id 保存
    for idx, item in enumerate(kept_items):
        item["id"] = idx

    kept_items.sort(key=lambda x: x.get("modified_at", ""), reverse=True)

    # 排序后重新计算 source_index
    source_by_key = {}
    for idx, item in enumerate(kept_items):
        item["id"] = idx
        key = f"{item.get('owner_username', '')}::{item.get('path', '')}"
        source_by_key[key] = idx

    visible_items = []
    for item in kept_items:
        if not data_file_belongs_to_user(item, username):
            continue

        row = dict(item)
        key = f"{row.get('owner_username', '')}::{row.get('path', '')}"
        row["_source_index"] = source_by_key.get(key, -1)
        visible_items.append(row)

    # 前端看到的是当前用户自己的 0,1,2...
    for idx, item in enumerate(visible_items):
        item["id"] = idx

    save_data_files(kept_items)
    return kept_items, visible_items


def get_user_data_file_by_visible_id(file_id: int, username: str) -> tuple[list[dict], int, dict]:
    all_items, visible_items = load_visible_data_files_for_user(username)

    if file_id < 0 or file_id >= len(visible_items):
        raise HTTPException(status_code=404, detail="文件不存在")

    visible_item = visible_items[file_id]
    source_index = int(visible_item.get("_source_index", -1))

    if source_index < 0 or source_index >= len(all_items):
        raise HTTPException(status_code=404, detail="文件不存在")

    item = all_items[source_index]

    if not data_file_belongs_to_user(item, username):
        raise HTTPException(status_code=404, detail="文件不存在")

    return all_items, source_index, item

def get_data_file_by_id_with_permission(file_id: int, user) -> tuple[list[dict], int, dict]:
    username = get_username_from_user(user)

    if isinstance(user, dict):
        role = str(user.get("role") or "")
    else:
        role = str(getattr(user, "role", "") or "")

    # 管理员：按全局 id 访问全部文件
    if role == "admin":
        all_items, _ = load_visible_data_files_for_user(username)

        if file_id < 0 or file_id >= len(all_items):
            raise HTTPException(status_code=404, detail="文件不存在")

        item = all_items[file_id]
        return all_items, file_id, item

    # 普通用户：只能访问自己的 visible id
    return get_user_data_file_by_visible_id(file_id, username)
@app.post("/api/tasks/run")
def api_run_module(payload: ModuleRunRequest, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    username = get_username_from_user(user)
    if not username:
        raise HTTPException(status_code=401, detail="未登录")

    module = get_module(payload.module_id)
    if not module:
        raise HTTPException(status_code=404, detail="模块不存在")
    if not module.get("enabled", True):
        raise HTTPException(status_code=400, detail="模块已禁用")

    # 直接使用前端传来的用户选择路径；管理员固定输入在这里补齐。
    inputs = merge_admin_fixed_inputs(module, payload.inputs or {})
    inputs = coerce_json_marked_inputs(module, inputs)
    parallel_workers = clamp_parallel_workers(payload.parallel_workers, task_manager.max_process_slots)

    # 必填校验。
    for field in module.get("inputs", []) or []:
        key = field.get("key")
        required = field.get("required", False)
        if not key:
            continue
        if field.get("control_only") is True:
            continue
        if required and (key not in inputs or inputs.get(key) in ("", None)):
            raise HTTPException(status_code=400, detail=f"缺少必填参数: {key}")

    # control_only 字段不写入 config.json。
    for field in module.get("inputs", []) or []:
        if field.get("control_only") is True:
            key = field.get("key")
            if key:
                inputs.pop(key, None)

    # 输出目录只负责创建，不改写用户选择的路径。
    for field in module.get("inputs", []) or []:
        key = field.get("key")
        if not key or not is_output_field(field):
            continue

        value = str(inputs.get(key) or "").strip()
        if not value:
            continue

        field_type = str(field.get("type", "")).lower()
        p = Path(value)
        if field_type == "dir_path":
            p.mkdir(parents=True, exist_ok=True)
        elif field_type == "file_path" and p.parent:
            p.parent.mkdir(parents=True, exist_ok=True)

    # 旧系统进程池批处理：只要识别到批处理目录，就算进程数是 1，也先拆成具体文件 job。
    if _is_batch_request(module, inputs):
        jobs, output_paths = build_batch_jobs_for_module(module, inputs, parallel_workers)
        task = task_manager.submit_batch_group(
            module_id=module["id"],
            module_name=module.get("name", module["id"]),
            jobs=jobs,
            max_parallel=parallel_workers,
            owner_username=username,
        )
        if output_paths:
            start_data_file_scan_after_task(
                task["id"],
                module,
                output_paths,
                owner_username=username,
            )
        return task
        return task

    # 非批处理模块：如果模块内部自己处理并行，则把并行数传给模块。
    if parallel_workers > 1:
        inputs = dict(inputs)
        inputs["parallel_workers"] = parallel_workers
        inputs["_parallel_workers"] = parallel_workers

    output_paths = collect_output_paths_from_inputs(module, inputs)

    command, working_dir, runtime_env = build_runtime_for_module(module, inputs)

    task = task_manager.submit_module_task(
        module_id=module["id"],
        module_name=module.get("name", module["id"]),
        command=command,
        inputs=inputs,
        working_dir=working_dir,
        env=runtime_env,
        owner_username=username,
    )

    if output_paths:
        start_data_file_scan_after_task(
            task["id"],
            module,
            output_paths,
            owner_username=username,
        )

    return task
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


def _is_path_inside(child: Path, parent: Path) -> bool:
    """
    判断 child 是否在 parent 目录内部，避免误删项目外部目录。
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False

def _remove_path_safely(path: Path, allowed_roots: list[Path]) -> dict:
    """
    只允许删除指定安全目录下的文件或文件夹。
    Windows 下目录删除使用 safe_rmtree，避免只读文件或短暂占用直接失败。
    """
    path = Path(path).resolve()

    if not path.exists():
        return {
            "path": str(path),
            "status": "missing",
        }

    if not any(_is_path_inside(path, root) for root in allowed_roots):
        raise HTTPException(
            status_code=400,
            detail=f"拒绝删除非模块目录路径: {path}",
        )

    if path.is_dir():
        safe_rmtree(path)
    else:
        last_error = None

        for _ in range(5):
            try:
                os.chmod(path, stat.S_IWRITE)
                path.unlink()
                break
            except FileNotFoundError:
                break
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.4)
            except OSError as exc:
                last_error = exc
                time.sleep(0.4)
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"删除模块本地文件失败: {path}\n"
                    f"原因: {last_error}\n"
                    "请关闭正在使用该文件的程序或停止相关任务后重试。"
                ),
            )

    return {
        "path": str(path),
        "status": "deleted",
    }

def _remove_readonly_or_locked(func, path, exc_info):
    """
    Windows 删除模块目录时，遇到只读文件时先改权限再删。
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        raise


def safe_rmtree(path: Path, retries: int = 5, delay: float = 0.4):
    """
    更稳的目录删除：
    1. 支持只读文件；
    2. 对 Windows 短暂占用做重试；
    3. 最后失败时给出明确错误。
    """
    path = Path(path)

    if not path.exists():
        return

    last_error = None

    for _ in range(retries):
        try:
            shutil.rmtree(path, onerror=_remove_readonly_or_locked)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(delay)
        except OSError as exc:
            last_error = exc
            time.sleep(delay)

    raise HTTPException(
        status_code=400,
        detail=(
            f"删除模块本地文件失败: {path}\n"
            f"原因: {last_error}\n"
            "请关闭正在使用该模块文件的程序或停止相关任务后重试。"
        ),
    )
def remove_module(module_id: str) -> dict:
    """
    删除模块：
    1. 先从 modules.json 移除模块记录；
    2. 再尝试删除 backend/installed_modules/{module_id}；
    3. 再尝试删除 backend/module_envs/{module_id}；
    4. 如果本地文件被占用，模块记录仍然删除成功，只返回 cleanup_warning。
    """
    modules = load_modules()

    target_module = None
    new_modules = []

    for module in modules:
        if module.get("id") == module_id:
            target_module = module
        else:
            new_modules.append(module)

    if not target_module:
        return {
            "removed": False,
            "deleted_paths": [],
            "cleanup_warnings": [],
        }

    safe_module_id = sanitize_filename(module_id).strip()
    if not safe_module_id:
        raise HTTPException(status_code=400, detail="模块 ID 不能为空")

    # 先删 modules.json 记录，避免本地文件被占用时界面一直删不掉模块。
    save_modules(new_modules)

    installed_dir = INSTALLED_MODULES_DIR / safe_module_id
    env_dir = PYTHON_MODULE_ENVS_DIR / safe_module_id

    allowed_roots = [
        INSTALLED_MODULES_DIR,
        PYTHON_MODULE_ENVS_DIR,
    ]

    deleted_paths = []
    cleanup_warnings = []

    for path in [installed_dir, env_dir]:
        try:
            deleted_paths.append(_remove_path_safely(path, allowed_roots))
        except HTTPException as exc:
            cleanup_warnings.append(str(exc.detail))
        except Exception as exc:
            cleanup_warnings.append(f"清理失败: {path}，原因: {exc}")

    return {
        "removed": True,
        "module_id": module_id,
        "deleted_paths": deleted_paths,
        "cleanup_warnings": cleanup_warnings,
    }
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


def resolve_module_dir(module: dict) -> Path:
    working_dir = module.get("working_dir", ".")
    project_root = BASE_DIR.parent
    module_dir = Path(working_dir)
    if not module_dir.is_absolute():
        module_dir = (project_root / module_dir).resolve()
    else:
        module_dir = module_dir.resolve()
    return module_dir



def to_module_json_value(value: Any) -> Any:
    """把写给模块 exe 的 config 值整理成更兼容的 JSON。

    Windows 下很多 C/C++ 程序用简单字符串方式解析 JSON，不会处理反斜杠转义。
    因此写给模块的路径统一使用正斜杠 /，同时递归处理 dict/list。
    """
    if isinstance(value, dict):
        return {str(k): to_module_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_module_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [to_module_json_value(v) for v in value]
    if isinstance(value, Path):
        return str(value).replace("\\", "/")
    if isinstance(value, str):
        return value.replace("\\", "/")
    return value


def resolve_input_value_for_module(module: dict, field: dict, value: Any) -> Any:
    if value in (None, ""):
        return value
    if field.get("path_mode") == "relative_to_module" and field.get("type") in {"file_path", "dir_path"}:
        p = Path(str(value))
        if not p.is_absolute():
            return str((resolve_module_dir(module) / p).resolve())
    return value


def merge_admin_fixed_inputs(module: dict, inputs: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(inputs or {})
    for field in module.get("inputs", []) or []:
        key = field.get("key")
        if not key:
            continue
        visible = field.get("visible_to_user", True) is not False
        admin_fixed = bool(field.get("admin_fixed", False)) or not visible
        has_user_value = key in merged and merged.get(key) not in ("", None)
        default_value = field.get("default")

        if admin_fixed or not has_user_value:
            if default_value not in ("", None):
                merged[key] = resolve_input_value_for_module(module, field, default_value)
        else:
            merged[key] = resolve_input_value_for_module(module, field, merged.get(key))
    return merged



def coerce_json_marked_inputs(module: dict, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """把自动识别出的复杂 JSON 参数从字符串还原成 dict/list/number/bool。"""
    result = dict(inputs or {})
    for field in module.get("inputs", []) or []:
        key = field.get("key")
        if not key or key not in result:
            continue

        if field.get("json_value") is True and isinstance(result.get(key), str):
            raw = result.get(key, "")
            if raw == "":
                continue
            try:
                result[key] = json.loads(raw)
            except Exception:
                # 用户在文本框里填的不是合法 JSON 时，保留原字符串，避免任务直接崩溃。
                result[key] = raw

    return result

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
    values["python_executable"] = str(exe_path)
    values["working_dir"] = str(module_dir)

    entry_script = str(module.get("entry_script") or "").strip()
    if entry_script:
        entry_path = Path(entry_script)
        if not entry_path.is_absolute():
            entry_path = (project_root / entry_path).resolve()
        else:
            entry_path = entry_path.resolve()
        if not entry_path.exists():
            raise HTTPException(status_code=400, detail=f"Python 入口脚本不存在: {entry_path}")
        values["entry_script"] = str(entry_path)

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

    runtime_type_for_env = str(module.get("runtime_type") or "").lower()

    if runtime_type_for_env == "python_venv":
        try:
            venv_python = exe_path
            venv_root = venv_python.parent.parent

            venv_dirs = [
                venv_python.parent,
                venv_root,
                venv_root / "DLLs",
                venv_root / "Library" / "bin",
            ]

            site_packages = venv_root / "Lib" / "site-packages"

            if site_packages.exists():
                for libs_dir in site_packages.glob("*.libs"):
                    if libs_dir.is_dir():
                        venv_dirs.append(libs_dir)

                for libs_dir in [
                    site_packages / "h5py.libs",
                    site_packages / "numpy.libs",
                    site_packages / "osgeo",
                ]:
                    if libs_dir.exists() and libs_dir.is_dir():
                        venv_dirs.append(libs_dir)

            for item in venv_dirs:
                if item.exists() and item.is_dir():
                    resolved_item = str(item.resolve())
                    if resolved_item not in seen:
                        ordered_dirs.append(resolved_item)
                        seen.add(resolved_item)
        except Exception:
            pass

    runtime_env["PATH"] = ";".join(ordered_dirs + [runtime_env.get("PATH", "")])
    runtime_env["MODULE_DLL_DIRS"] = ";".join(ordered_dirs)

    # 避免 OpenBLAS 线程过多导致崩溃
    runtime_env["OPENBLAS_NUM_THREADS"] = "1"
    runtime_env["OMP_NUM_THREADS"] = "1"
    runtime_env["GOTO_NUM_THREADS"] = "1"

    # 便于排查 DLL 搜索路径
    runtime_env["MODULE_DLL_DIRS"] = ";".join(ordered_dirs)

    if config_mode in {"json", "json_file", "config_json"}:
        runtime_task_dir = Path(tempfile.mkdtemp(prefix="job_", dir=str(RUNTIME_DIR)))
        module_config = to_module_json_value(inputs)

        runtime_type = str(module.get("runtime_type") or "").lower()

        if runtime_type == "python_venv":
            source_dir = Path(str(module.get("source_dir") or module.get("working_dir") or ""))

            if not source_dir.is_absolute():
                source_dir = (project_root / source_dir).resolve()
            else:
                source_dir = source_dir.resolve()

            if not source_dir.exists() or not source_dir.is_dir():
                raise HTTPException(status_code=400, detail=f"Python 源码目录不存在: {source_dir}")

            run_source_dir = runtime_task_dir / "source"
            shutil.copytree(
                source_dir,
                run_source_dir,
                ignore=shutil.ignore_patterns(
                    "__pycache__",
                    "*.pyc",
                    ".venv",
                    "venv",
                    ".git",
                ),
            )

            config_path = run_source_dir / "config.json"
            config_path.write_text(
                json.dumps(module_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            entry_name = str(module.get("entry_file") or "main.py")
            entry_path = run_source_dir / entry_name
            if not entry_path.exists():
                candidates = list(run_source_dir.rglob(entry_name))
                if candidates:
                    entry_path = candidates[0]

            if not entry_path.exists():
                raise HTTPException(status_code=400, detail=f"Python 入口脚本不存在: {entry_name}")

            values["config_json"] = str(config_path)
            values["config_path"] = str(config_path)
            values["runtime_dir"] = str(runtime_task_dir)
            values["entry_script"] = str(entry_path)

            module_dir = run_source_dir

            if not command_template:
                command_template = ["{executable}", "{entry_script}"]

        else:
            config_path = runtime_task_dir / "config.json"
            config_path.write_text(
                json.dumps(module_config, ensure_ascii=False, indent=2),
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
def build_python_source_to_exe(
    source_zip: Path,
    module_id: str,
    entry_file: str = "main.py",
) -> tuple[Path, Path]:
    """
    把用户上传的 Python 源码 zip 打包成 exe。

    返回：
    - module_root: 解压后的源码目录
    - exe_path: 生成的 exe 路径
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="python_module_"))

    try:
        source_dir = temp_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(source_zip, "r") as zf:
            zf.extractall(source_dir)

        entry_path = source_dir / entry_file
        if not entry_path.exists():
            candidates = list(source_dir.rglob(entry_file))
            if candidates:
                entry_path = candidates[0]

        if not entry_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"未找到 Python 入口文件：{entry_file}",
            )

        requirements_path = source_dir / "requirements.txt"

        # 可选：如果源码包里有 requirements.txt，先安装依赖
        if requirements_path.exists():
            install_requirements_with_local_wheels(
                python_exe=Path(sys.executable),
                requirements_path=requirements_path,
                work_dir=source_dir,
            )

        dist_dir = temp_dir / "dist"
        build_dir = temp_dir / "build"
        spec_dir = temp_dir / "spec"

        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            module_id,
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(build_dir),
            "--specpath",
            str(spec_dir),
            str(entry_path),
        ]

        result = subprocess.run(
            cmd,
            cwd=str(source_dir),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Python 代码打包失败：\n"
                    + (result.stderr or result.stdout or "未知错误")
                ),
            )

        exe_path = dist_dir / f"{module_id}.exe"
        if not exe_path.exists():
            raise HTTPException(
                status_code=400,
                detail="打包完成但未找到生成的 exe 文件",
            )

        return source_dir, exe_path

    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=400,
            detail=f"安装 Python 依赖失败：{e}",
        )


def _resolve_local_json_path(raw_path: str) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="参数 JSON 文件路径不能为空")

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    else:
        path = path.resolve()

    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=400, detail=f"参数 JSON 文件不存在: {path}")
    if path.suffix.lower() != ".json":
        raise HTTPException(status_code=400, detail="请选择 .json 参数文件")
    return path


def load_param_json_file(raw_path: str) -> dict:
    path = _resolve_local_json_path(raw_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        data = json.loads(path.read_text(encoding="gbk"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"参数 JSON 解析失败: {exc}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="参数 JSON 顶层必须是对象，例如 {\"input_dir\": \"...\"}")
    return data

def _resolve_path_relative_to_config(raw_path: str, config_path: Path) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="路径不能为空")

    path = Path(raw).expanduser()

    if not path.is_absolute():
        path = (config_path.parent / path).resolve()
    else:
        path = path.resolve()

    return path


def load_python_module_config(raw_path: str) -> tuple[dict, Path]:
    config_path = _resolve_local_json_path(raw_path)

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        data = json.loads(config_path.read_text(encoding="gbk"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Python 模块配置 JSON 解析失败: {exc}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Python 模块配置 JSON 顶层必须是对象")

    # 兼容两种写法：
    # 1. 直接平铺字段：module_id/source_dir/...
    # 2. 写在 module 下面：{"module": {...}}
    module_cfg = data.get("module") if isinstance(data.get("module"), dict) else data

    module_id = str(module_cfg.get("module_id") or module_cfg.get("id") or "").strip()
    module_name = str(module_cfg.get("module_name") or module_cfg.get("name") or "").strip()
    source_dir_raw = str(module_cfg.get("source_dir") or module_cfg.get("python_source_dir") or "").strip()
    entry_file = str(module_cfg.get("entry_file") or "main.py").strip()
    tool_type = str(module_cfg.get("tool_type") or "").strip()
    description = str(module_cfg.get("description") or "").strip()
    python_executable = str(
        module_cfg.get("python_executable")
        or module_cfg.get("python")
        or module_cfg.get("python_path")
        or ""
    ).strip()
    if not module_id:
        raise HTTPException(status_code=400, detail="Python 模块配置 JSON 缺少 module_id")
    if not module_name:
        raise HTTPException(status_code=400, detail="Python 模块配置 JSON 缺少 module_name")
    if not source_dir_raw:
        raise HTTPException(status_code=400, detail="Python 模块配置 JSON 缺少 source_dir")

    source_dir = _resolve_path_relative_to_config(source_dir_raw, config_path)

    if not source_dir.exists() or not source_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Python 源码文件夹不存在: {source_dir}")

    param_template = module_cfg.get("param_template")
    param_json_path = None

    if isinstance(param_template, dict):
        param_json = param_template
    else:
        param_json_raw = str(module_cfg.get("param_json_path") or module_cfg.get("config_json") or "").strip()
        if not param_json_raw:
            # 默认找源码目录下的 config.json
            param_json_path = source_dir / "config.json"
        else:
            param_json_path = _resolve_path_relative_to_config(param_json_raw, config_path)

        if not param_json_path.exists() or not param_json_path.is_file():
            raise HTTPException(status_code=400, detail=f"参数 JSON 文件不存在: {param_json_path}")

        param_json = load_param_json_file(str(param_json_path))

    return {
        "module_id": module_id,
        "module_name": module_name,
        "source_dir": str(source_dir),
        "entry_file": entry_file,
        "tool_type": tool_type,
        "description": description,
        "param_json_path": str(param_json_path) if param_json_path else "",
        "param_json": param_json,
        "python_executable": python_executable,
    }, config_path
def install_python_venv_module_from_values(
    module_id: str,
    module_name: str,
    source_dir: str,
    entry_file: str,
    tool_type: str = "",
    description: str = "",
    param_json_path: str = "",
    param_json: dict | None = None,
    python_executable: str = "",
) -> dict:
    safe_module_id = sanitize_filename(module_id).strip()
    if not safe_module_id:
        raise HTTPException(status_code=400, detail="模块 ID 不能为空")

    source_root = Path(source_dir or "").expanduser()
    if not source_root.is_absolute():
        source_root = (PROJECT_ROOT / source_root).resolve()
    else:
        source_root = source_root.resolve()

    if not source_root.exists() or not source_root.is_dir():
        raise HTTPException(status_code=400, detail=f"Python 源码文件夹不存在: {source_root}")

    if param_json is None:
        if not param_json_path:
            param_json_path = str(source_root / "config.json")
        resolved_param_json_path = _resolve_local_json_path(param_json_path)
        param_json = load_param_json_file(str(resolved_param_json_path))
    else:
        resolved_param_json_path = None

    inferred_inputs = infer_inputs_from_param_json(param_json)

    entry_name = entry_file or "main.py"
    entry_candidate = source_root / entry_name
    if not entry_candidate.exists():
        candidates = list(source_root.rglob(entry_name))
        if candidates:
            entry_candidate = candidates[0]

    if not entry_candidate.exists() or not entry_candidate.is_file():
        raise HTTPException(status_code=400, detail=f"未找到 Python 入口文件: {entry_name}")

    target_dir = INSTALLED_MODULES_DIR / safe_module_id
    if target_dir.exists():
        safe_rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    source_target_dir = target_dir / "source"
    shutil.copytree(source_root, source_target_dir, dirs_exist_ok=True)

    # 保存参数模板
    param_template_path = target_dir / "param_template.json"
    param_template_path.write_text(
        json.dumps(param_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rel_entry = entry_candidate.resolve().relative_to(source_root.resolve())
    installed_entry_script = source_target_dir / rel_entry

    python_exe = create_python_module_env(
        safe_module_id,
        source_target_dir,
        base_python_executable=python_executable,
    )

    module_json_candidates = list(source_target_dir.rglob("module.json"))
    if module_json_candidates:
        module_data = json.loads(module_json_candidates[0].read_text(encoding="utf-8"))
    else:
        module_data = {
            "id": safe_module_id,
            "name": module_name or safe_module_id,
            "description": description or "Python 源码独立环境运行模块",
            "enabled": True,
        }

    selected_tool_type = (
        normalize_tool_key(tool_type or module_data.get("tool_type") or "")
        or guess_module_tool_type(module_data)
    )

    module_data["id"] = safe_module_id
    module_data["name"] = module_name or module_data.get("name") or safe_module_id
    module_data["description"] = description or module_data.get("description") or "Python 源码独立环境运行模块"
    module_data["tool_type"] = selected_tool_type
    module_data["enabled"] = module_data.get("enabled", True)
    module_data["runtime_type"] = "python_venv"
    module_data["config_mode"] = "config_json"
    module_data["command_template"] = ["{executable}", "{entry_script}", "{config_json}"]
    module_data["inputs"] = inferred_inputs
    module_data["param_template"] = to_project_relative_path(param_template_path)
    module_data["source_dir"] = to_project_relative_path(source_target_dir)
    module_data["entry_file"] = rel_entry.as_posix()
    module_data["entry_script"] = to_project_relative_path(installed_entry_script)
    module_data["python_env_dir"] = to_project_relative_path(PYTHON_MODULE_ENVS_DIR / safe_module_id)
    module_data["base_python_executable"] = python_executable
    module_data["executable"] = to_project_relative_path(python_exe)
    module_data["working_dir"] = to_project_relative_path(source_target_dir)

    ensure_toolbar_exists(selected_tool_type)
    upsert_module(module_data)

    return module_data

def infer_param_input_type(key: str, value: Any) -> str:
    k = str(key or "").lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    if isinstance(value, (dict, list)):
        return "textarea"

    text = str(value or "")
    suffix = Path(text).suffix.lower() if text else ""
    if any(x in k for x in ["dir", "folder", "目录", "outpath", "out_dir", "output_dir", "输出目录"]):
        return "dir_path"
    if any(x in k for x in ["file", "path", "文件"]):
        if suffix:
            return "file_path"
        return "dir_path"
    if suffix in {".tif", ".tiff", ".nc", ".hdf", ".h5", ".json", ".txt", ".xml", ".dat", ".csv"}:
        return "file_path"
    return "text"


def infer_inputs_from_param_json(data: dict) -> list[dict]:
    inputs: list[dict] = []
    for key, value in data.items():
        if key in {"executable", "working_dir", "config_json", "config_path", "runtime_dir"}:
            continue

        field_type = infer_param_input_type(key, value)
        item: dict[str, Any] = {
            "key": str(key),
            "label": str(key),
            "type": field_type,
            "required": True,
            "visible_to_user": True,
            "admin_fixed": False,
            "path_mode": "absolute",
            "io_role": "auto",
        }

        lower_key = str(key).lower()
        if any(x in lower_key for x in ["out", "output", "result", "save", "输出"]):
            item["io_role"] = "output"
        elif field_type in {"file_path", "dir_path"}:
            item["io_role"] = "input"

        if isinstance(value, (dict, list)):
            item["default"] = json.dumps(value, ensure_ascii=False, indent=2)
            item["json_value"] = True
            item["help_text"] = "复杂 JSON 参数，运行前会自动还原为对象或数组"
        elif value is not None:
            item["default"] = value

        inputs.append(item)
    return inputs


def build_python_source_dir_to_exe(
    source_dir_path: Path,
    module_id: str,
    entry_file: str = "main.py",
) -> tuple[Path, Path]:
    """把本地 Python 源码文件夹复制到临时目录后打包为 exe。"""
    if not source_dir_path.exists() or not source_dir_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Python 源码文件夹不存在: {source_dir_path}")

    temp_dir = Path(tempfile.mkdtemp(prefix="python_module_folder_"))
    source_dir = temp_dir / "source"
    shutil.copytree(source_dir_path, source_dir, dirs_exist_ok=True)

    entry_path = source_dir / entry_file
    if not entry_path.exists():
        candidates = list(source_dir.rglob(entry_file))
        if candidates:
            entry_path = candidates[0]

    if not entry_path.exists():
        raise HTTPException(status_code=400, detail=f"未找到 Python 入口文件: {entry_file}")

    requirements_path = source_dir / "requirements.txt"
    if requirements_path.exists():
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(requirements_path)],
                cwd=str(source_dir),
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise HTTPException(status_code=400, detail=f"安装 Python 依赖失败: {exc}")

    dist_dir = temp_dir / "dist"
    build_dir = temp_dir / "build"
    spec_dir = temp_dir / "spec"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            module_id,
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(build_dir),
            "--specpath",
            str(spec_dir),
            str(entry_path),
        ],
        cwd=str(source_dir),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail="Python 代码打包失败:\n" + (result.stderr or result.stdout or "未知错误"),
        )

    exe_path = dist_dir / f"{module_id}.exe"
    if not exe_path.exists():
        raise HTTPException(status_code=400, detail="打包完成但未找到生成的 exe 文件")

    return source_dir, exe_path

def get_venv_python_path(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"
def parse_requirement_package_name(line: str) -> str:
    """
    从 requirements.txt 的一行里解析包名。
    例如：
    GDAL==3.4.3 -> gdal
    numpy>=1.23 -> numpy
    h5py -> h5py
    """
    text = (line or "").strip()

    if not text or text.startswith("#"):
        return ""

    if text.startswith("-"):
        return ""

    for sep in ["==", ">=", "<=", "~=", "!=", ">", "<", "[", ";"]:
        if sep in text:
            text = text.split(sep, 1)[0]
            break

    return text.strip().lower().replace("_", "-")


def split_requirements_for_local_binary(requirements_path: Path) -> tuple[list[str], list[str], list[str]]:
    strict_specs: list[str] = []
    prefer_specs: list[str] = []
    normal_lines: list[str] = []

    for raw in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            normal_lines.append(raw)
            continue

        pkg = parse_requirement_package_name(line)

        if pkg in STRICT_LOCAL_BINARY_PACKAGES:
            strict_specs.append(line)
        elif pkg in PREFER_LOCAL_BINARY_PACKAGES:
            prefer_specs.append(line)
        else:
            normal_lines.append(raw)

    return strict_specs, prefer_specs, normal_lines
def run_checked_command(
    cmd: list[str],
    cwd: Path | None = None,
    title: str = "执行命令",
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )

    if result.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{title}失败：\n"
                f"命令：{' '.join(map(str, cmd))}\n\n"
                f"STDOUT:\n{result.stdout or ''}\n\n"
                f"STDERR:\n{result.stderr or ''}"
            ),
        )

    return result
def install_requirements_with_local_wheels(
    python_exe: Path,
    requirements_path: Path,
    work_dir: Path,
):
    if not requirements_path.exists():
        return

    strict_specs, prefer_specs, normal_lines = split_requirements_for_local_binary(requirements_path)

    # numpy / h5py：优先本地 wheel，但不强制本地。
    for spec in prefer_specs:
        run_checked_command(
            [
                str(python_exe),
                "-m",
                "pip",
                "install",
                "--only-binary",
                ":all:",
                "--prefer-binary",
                "--find-links",
                str(PYTHON_WHEELS_DIR),
                spec,
            ],
            cwd=work_dir,
            title=f"安装二进制依赖 {spec}",
        )

    # GDAL / rasterio / pyproj / cartopy：强制从本地 wheel 安装，避免源码编译失败。
    for spec in strict_specs:
        run_checked_command(
            [
                str(python_exe),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--only-binary",
                ":all:",
                "--find-links",
                str(PYTHON_WHEELS_DIR),
                spec,
            ],
            cwd=work_dir,
            title=f"从本地二进制包安装 {spec}",
        )

    normal_req_path = work_dir / "requirements.normal.txt"
    normal_req_path.write_text(
        "\n".join(normal_lines),
        encoding="utf-8",
    )

    if normal_req_path.read_text(encoding="utf-8").strip():
        run_checked_command(
            [
                str(python_exe),
                "-m",
                "pip",
                "install",
                "--prefer-binary",
                "--find-links",
                str(PYTHON_WHEELS_DIR),
                "-r",
                str(normal_req_path),
            ],
            cwd=work_dir,
            title="安装普通 Python 依赖",
        )
def create_python_module_env(
    module_id: str,
    source_dir: Path,
    base_python_executable: str = "",
) -> Path:
    """为 Python 源码模块创建独立 venv，并安装 requirements.txt。"""
    env_dir = PYTHON_MODULE_ENVS_DIR / module_id

    if env_dir.exists():
        safe_rmtree(env_dir)

    # 用 --clear 明确创建干净环境，--copies 在 Windows 上比软链接更稳
    if base_python_executable:
        base_python = Path(base_python_executable).expanduser()

        if not base_python.is_absolute():
            base_python = (PROJECT_ROOT / base_python).resolve()
        else:
            base_python = base_python.resolve()

        if not base_python.exists() or not base_python.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"指定的 Python 解释器不存在: {base_python}",
            )
    else:
        base_python = Path(sys.executable).resolve()

    run_checked_command(
        [str(base_python), "-m", "venv", "--clear", "--copies", str(env_dir)],
        cwd=BASE_DIR,
        title=f"创建 Python 独立环境，基础解释器: {base_python}",
    )

    python_exe = get_venv_python_path(env_dir)
    run_checked_command(
        [
            str(python_exe),
            "-c",
            "import sys, struct, platform; print(sys.version); print(struct.calcsize('P')*8); print(platform.machine())",
        ],
        cwd=source_dir,
        title="检查模块 Python 版本",
    )
    if not python_exe.exists():
        raise HTTPException(status_code=400, detail=f"创建环境后未找到 Python 解释器: {python_exe}")

    # 关键：先用 ensurepip 修复/安装 pip。
    # 不依赖当前 venv 里已经损坏的 pip 包。
    run_checked_command(
        [str(python_exe), "-m", "ensurepip", "--upgrade", "--default-pip"],
        cwd=source_dir,
        title="初始化 pip",
    )

    # 检查 pip 是否真的可用
    run_checked_command(
        [str(python_exe), "-m", "pip", "--version"],
        cwd=source_dir,
        title="检查 pip",
    )

    # 再升级基础打包工具。这里不要只升级 pip，也顺带装 setuptools / wheel。
    run_checked_command(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--force-reinstall",
            "pip",
            "setuptools",
            "wheel",
        ],
        cwd=source_dir,
        title="升级 pip/setuptools/wheel",
    )

    requirements_path = source_dir / "requirements.txt"
    if requirements_path.exists():
        install_requirements_with_local_wheels(
            python_exe=python_exe,
            requirements_path=requirements_path,
            work_dir=source_dir,
        )

    return python_exe


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

        selected_tool_type = (
            normalize_tool_key(tool_type or module_data.get("tool_type") or "")
            or guess_module_tool_type(module_data)
        )
        module_data["tool_type"] = selected_tool_type
        ensure_toolbar_exists(selected_tool_type)

        module_id = str(module_data.get("id") or "").strip()
        if not module_id:
            raise HTTPException(status_code=400, detail="module.json 缺少 id")

        target_dir = INSTALLED_MODULES_DIR / module_id

        if target_dir.exists():
            safe_rmtree(target_dir)

        shutil.copytree(module_root, target_dir)

        # executable 保存为项目相对路径，不再保存 D:/... 这种绝对路径
        executable = str(module_data.get("executable") or "").strip()
        if executable:
            exe_path = resolve_packaged_module_path(
                raw_value=executable,
                module_id=module_id,
                target_dir=target_dir,
                default_path=target_dir,
            )

            module_data["executable"] = to_project_relative_path(exe_path)

        # working_dir 保存为项目相对路径
        working_dir = str(module_data.get("working_dir") or ".").strip()
        wd_path = resolve_packaged_module_path(
            raw_value=working_dir,
            module_id=module_id,
            target_dir=target_dir,
            default_path=target_dir,
        )

        module_data["working_dir"] = to_project_relative_path(wd_path)

        upsert_module(module_data)
        return module_data

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

# ========================
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
def install_module_from_folder(folder_path: Path, tool_type: str | None = None) -> dict:
    if not folder_path.exists() or not folder_path.is_dir():
        raise HTTPException(status_code=400, detail=f"模块文件夹不存在: {folder_path}")

    module_json_path = folder_path / "module.json"
    if not module_json_path.exists():
        candidates = list(folder_path.rglob("module.json"))
        if not candidates:
            raise HTTPException(status_code=400, detail="模块文件夹中未找到 module.json")
        module_json_path = candidates[0]

    module_root = module_json_path.parent
    module_data = json.loads(module_json_path.read_text(encoding="utf-8"))

    selected_tool_type = (
        normalize_tool_key(tool_type or module_data.get("tool_type") or "")
        or guess_module_tool_type(module_data)
    )

    module_data["tool_type"] = selected_tool_type
    ensure_toolbar_exists(selected_tool_type)

    module_id = str(module_data.get("id") or "").strip()
    if not module_id:
        raise HTTPException(status_code=400, detail="module.json 缺少 id")

    target_dir = INSTALLED_MODULES_DIR / module_id

    if target_dir.exists():
        safe_rmtree(target_dir)

    shutil.copytree(module_root, target_dir)

    executable = str(module_data.get("executable") or "").strip()
    if executable:
        exe_path = resolve_packaged_module_path(
            raw_value=executable,
            module_id=module_id,
            target_dir=target_dir,
            default_path=target_dir,
        )
        module_data["executable"] = to_project_relative_path(exe_path)

    working_dir = str(module_data.get("working_dir") or ".").strip()
    wd_path = resolve_packaged_module_path(
        raw_value=working_dir,
        module_id=module_id,
        target_dir=target_dir,
        default_path=target_dir,
    )
    module_data["working_dir"] = to_project_relative_path(wd_path)

    upsert_module(module_data)
    return module_data


@app.post("/api/admin/modules/install-folder")
def api_install_module_folder(
    payload: InstallModuleFolderRequest,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    module_data = install_module_from_folder(
        Path(payload.folder_path).expanduser().resolve(),
        payload.tool_type,
    )

    return {
        "ok": True,
        "message": "模块文件夹安装成功",
        "module": module_data,
    }
"""新增 /api/admin/modules/upload-python

这个接口负责：

接收 Python 源码 zip；
使用 PyInstaller 打包 exe；
放入 backend/installed_modules/{module_id}/；
自动生成或读取 module.json；
写入 modules.json。"""
@app.post("/api/admin/modules/upload-python")
def api_upload_python_module(
    file: UploadFile = File(...),
    module_id: str = Form(...),
    module_name: str = Form(...),
    entry_file: str = Form("main.py"),
    tool_type: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
):
    user = get_current_user(authorization)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以上传模块")

    safe_module_id = sanitize_filename(module_id).strip()
    if not safe_module_id:
        raise HTTPException(status_code=400, detail="模块 ID 不能为空")

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 Python 源码 zip 包")

    upload_tmp = Path(tempfile.mkdtemp(prefix="python_upload_"))

    try:
        zip_path = upload_tmp / sanitize_filename(file.filename)

        with zip_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)

        source_dir, exe_path = build_python_source_to_exe(
            source_zip=zip_path,
            module_id=safe_module_id,
            entry_file=entry_file,
        )

        target_dir = INSTALLED_MODULES_DIR / safe_module_id
        if target_dir.exists():
            safe_rmtree(target_dir)

        target_dir.mkdir(parents=True, exist_ok=True)

        # 复制源码，方便后续查看和维护
        source_target_dir = target_dir / "source"
        shutil.copytree(source_dir, source_target_dir, dirs_exist_ok=True)

        # 复制 exe
        final_exe_path = target_dir / f"{safe_module_id}.exe"
        shutil.copy2(exe_path, final_exe_path)

        # 如果源码包里有 module.json，优先读取
        module_json_candidates = list(source_dir.rglob("module.json"))
        if module_json_candidates:
            module_data = json.loads(module_json_candidates[0].read_text(encoding="utf-8"))
        else:
            module_data = {
                "id": safe_module_id,
                "name": module_name,
                "description": "Python 源码自动打包生成的模块",
                "enabled": True,
                "inputs": [],
            }

        selected_tool_type = (
            normalize_tool_key(tool_type or module_data.get("tool_type") or "")
            or guess_module_tool_type(module_data)
        )

        module_data["id"] = safe_module_id
        module_data["name"] = module_name or module_data.get("name") or safe_module_id
        module_data["tool_type"] = selected_tool_type
        module_data["enabled"] = module_data.get("enabled", True)

        ensure_toolbar_exists(selected_tool_type)

        # 关键：保存项目相对路径，不保存 D:/... 绝对路径
        module_data["executable"] = to_project_relative_path(final_exe_path)
        module_data["working_dir"] = to_project_relative_path(target_dir)

        upsert_module(module_data)

        return {
            "ok": True,
            "message": "Python 模块打包并安装成功",
            "module": module_data,
        }

    finally:
        shutil.rmtree(upload_tmp, ignore_errors=True)


@app.post("/api/admin/modules/upload-python-folder")
def api_upload_python_folder_module(
    payload: PythonFolderModuleUploadRequest,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    module_data = install_python_venv_module_from_values(
        module_id=payload.module_id,
        module_name=payload.module_name,
        source_dir=payload.source_dir,
        entry_file=payload.entry_file or "main.py",
        tool_type=payload.tool_type,
        description=payload.description,
        param_json_path=payload.param_json_path,
    )

    return {
        "ok": True,
        "message": "Python 源码模块已创建独立环境并安装成功",
        "module": module_data,
    }
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

    result = remove_module(module_id)

    if not result.get("removed"):
        raise HTTPException(status_code=404, detail="模块不存在")

    warnings = result.get("cleanup_warnings") or []

    return {
        "ok": True,
        "message": "模块记录已删除" if warnings else "模块及本地文件已删除",
        **result,
    }


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

@app.post("/api/admin/modules/parse-python-module-config")
def api_parse_python_module_config(
    payload: PythonModuleConfigRequest,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    config, config_path = load_python_module_config(payload.path)
    inputs = infer_inputs_from_param_json(config["param_json"])

    return {
        "ok": True,
        "config_path": str(config_path),
        "module": {
            "module_id": config["module_id"],
            "module_name": config["module_name"],
            "tool_type": config["tool_type"],
            "entry_file": config["entry_file"],
            "source_dir": config["source_dir"],
            "param_json_path": config["param_json_path"],
            "description": config["description"],
            "python_executable": config.get("python_executable") or "",
        },
        "inputs": inputs,
        "count": len(inputs),
    }


@app.post("/api/admin/modules/upload-python-config")
def api_upload_python_config_module(
    payload: PythonModuleConfigRequest,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    config, _ = load_python_module_config(payload.path)

    module_data = install_python_venv_module_from_values(
        module_id=config["module_id"],
        module_name=config["module_name"],
        source_dir=config["source_dir"],
        entry_file=config["entry_file"],
        tool_type=config["tool_type"],
        description=config["description"],
        param_json_path=config["param_json_path"],
        param_json=config["param_json"],
        python_executable=config.get("python_executable") or "",
    )

    return {
        "ok": True,
        "message": "Python 模块配置 JSON 安装成功",
        "module": module_data,
    }
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


def clamp_parallel_workers(value: int | str | None, max_workers: int | None = None) -> int:
    try:
        n = int(value or 1)
    except Exception:
        n = 1

    try:
        limit = int(max_workers or task_manager.max_process_slots or os.cpu_count() or 1)
    except Exception:
        limit = 1
    limit = max(1, limit)
    return max(1, min(n, limit))


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
    explicit = str(normalize_parallel_config(module).get("input_key") or "").strip()
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
    output_key = str(normalize_parallel_config(module).get("output_key") or "").strip()
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
    suffix = str(normalize_parallel_config(module).get("output_suffix") or ".tif")
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
    mode = str(normalize_parallel_config(module).get("mode") or "auto").strip() or "auto"
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

    patterns = parse_parallel_patterns(normalize_parallel_config(module).get("file_patterns"))
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
            return prepare_parallel_jobs({**module, "parallel": {**normalize_parallel_config(module), "mode": "single_file"}}, inputs, workers)

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
# 批处理进程池辅助函数
# =========================
BATCH_FILE_EXTS = {".tif", ".tiff", ".img", ".hdf", ".h5", ".nc", ".nc4", ".dat", ".json"}
SHARED_BATCH_MATCH_MODES = {
    "first",
    "shared",
    "fixed",
    "constant",
    "single",
    "reuse_first",
    "all_jobs",
}


def _infer_batch_role_from_field(field: dict) -> str:
    """显式 batch_role 优先；没有时从常见字段名自动推断 B01/B03/B06/SOLAR。

    这样旧模块或手工编辑时 batch_role 丢失，也不会把 B01 文件夹直接传给 exe。
    """
    explicit = str(field.get("batch_role") or "").strip()
    if explicit and explicit.upper() != "OUTPUT_DIR":
        return explicit

    if bool(field.get("control_only", False)):
        return ""

    if is_output_field(field):
        return ""

    field_type = str(field.get("type") or "").lower()
    if field_type not in {"dir_path", "file_path"}:
        return ""

    text = f"{field.get('key', '')} {field.get('label', '')}".upper()
    role_patterns = [
        ("B01", r"(^|[^A-Z0-9])B0?1([^A-Z0-9]|$)|B01_FILE|B01文件|B01 文件"),
        ("B03", r"(^|[^A-Z0-9])B0?3([^A-Z0-9]|$)|B03_FILE|B03文件|B03 文件"),
        ("B06", r"(^|[^A-Z0-9])B0?6([^A-Z0-9]|$)|B06_FILE|B06文件|B06 文件"),
        ("SOLAR", r"SOLAR|SUN|太阳角"),
    ]
    for role, pattern in role_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return role
    return ""


def _field_batch_role(field: dict) -> str:
    return _infer_batch_role_from_field(field)


def _is_shared_batch_field(field: dict) -> bool:
    mode = str(field.get("match_mode") or "").strip().lower()
    if mode in SHARED_BATCH_MATCH_MODES:
        return True
    if field.get("shared_across_jobs") is True:
        return True
    return False


def _parse_batch_patterns(field: dict) -> list[str]:
    """解析批处理文件匹配规则。

    说明：
    - 旧版默认只扫 tif/nc/hdf 等带扩展名文件；
    - PARASOL 原始输入常见为无扩展名文件，Windows 里显示“类型=文件”；
    - 所以 batch_role 批处理字段默认改为 "*"，并允许无扩展名文件进入。
    """
    raw = (
        field.get("file_patterns")
        or field.get("patterns")
        or field.get("pattern")
        or "*"
    )
    if isinstance(raw, list):
        patterns = [str(x).strip() for x in raw if str(x).strip()]
    else:
        patterns = [x.strip() for x in str(raw).replace(",", ";").split(";") if x.strip()]
    return patterns or ["*"]


def _pattern_means_all_files(patterns: list[str]) -> bool:
    normalized = {str(p or "").strip().replace("\\", "/") for p in patterns}
    return bool(normalized & {"*", "*.*", "**/*", "**/*.*"})


def _split_suffix_list(value: Any) -> set[str]:
    if value in (None, ""):
        return set()
    if isinstance(value, list):
        raw_items = value
    else:
        text = str(value).replace("；", ";").replace("，", ",")
        for sep in [",", "|", "\n", "\t", " "]:
            text = text.replace(sep, ";")
        raw_items = text.split(";")
    result: set[str] = set()
    for item in raw_items:
        suffix = str(item or "").strip().lower()
        if not suffix:
            continue
        if suffix == "*":
            result.add("*")
        elif suffix.startswith("."):
            result.add(suffix)
        else:
            result.add("." + suffix)
    return result


def _is_ignored_batch_file(path: Path, field: dict) -> bool:
    name = path.name
    if name.startswith("."):
        return True

    suffix = path.suffix.lower()

    default_ignored = {".tmp", ".bak", ".log", ".txt", ".json", ".xml"}
    extra_ignored = _split_suffix_list(field.get("batch_exclude_suffixes") or field.get("exclude_suffixes"))
    ignored = default_ignored | extra_ignored
    if suffix and suffix in ignored:
        return True

    exclude_regex = str(field.get("batch_exclude_regex") or "").strip()
    if exclude_regex:
        try:
            if re.search(exclude_regex, name, re.IGNORECASE):
                return True
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"batch_exclude_regex 正则错误: {exc}")

    return False


def _batch_file_allowed(path: Path, field: dict, patterns: list[str]) -> bool:
    """判断某个文件是否允许作为批处理输入。

    兼容规则：
    - field.batch_allow_all_files=true：除临时/日志/配置文件外全部允许；
    - patterns 包含 "*" 或 "*.*"：按全文件模式处理，允许无扩展名和未知扩展名；
    - 无扩展名文件默认允许，适配 PARASOL 原始文件；
    - 有扩展名时默认仍按 BATCH_FILE_EXTS 白名单控制。
    """
    if _is_ignored_batch_file(path, field):
        return False

    include_regex = str(field.get("batch_include_regex") or "").strip()
    if include_regex:
        try:
            if not re.search(include_regex, path.name, re.IGNORECASE):
                return False
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"batch_include_regex 正则错误: {exc}")

    suffix = path.suffix.lower()

    allowed_suffixes = _split_suffix_list(field.get("batch_suffixes") or field.get("allowed_suffixes"))
    if "*" in allowed_suffixes:
        return True
    if allowed_suffixes:
        if not suffix:
            return bool(field.get("batch_allow_no_extension", True))
        return suffix in allowed_suffixes

    if bool(field.get("batch_allow_all_files", False)):
        return True

    if _pattern_means_all_files(patterns):
        return True

    if not suffix:
        return bool(field.get("batch_allow_no_extension", True))

    return suffix in BATCH_FILE_EXTS


def _list_batch_files(value: str, field: dict) -> list[Path]:
    p = Path(str(value)).expanduser()
    if p.is_file():
        if _batch_file_allowed(p, field, ["*"]):
            return [p.resolve()]
        return []

    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"批量输入路径不存在或不是文件夹: {field.get('key')} -> {value}")

    patterns = _parse_batch_patterns(field)
    found: list[Path] = []
    seen: set[Path] = set()

    for pattern in patterns:
        for item in p.glob(pattern):
            if item.is_file() and _batch_file_allowed(item, field, patterns):
                rp = item.resolve()
                if rp not in seen:
                    seen.add(rp)
                    found.append(rp)

    # 兜底：如果用户写了 "*.*" 但文件没有扩展名，glob("*.*") 会扫不到。
    # 这里再扫一层目录，并套用同样的过滤规则。
    if not found:
        fallback_patterns = ["*"]
        for item in p.iterdir():
            if item.is_file() and _batch_file_allowed(item, field, fallback_patterns):
                rp = item.resolve()
                if rp not in seen:
                    seen.add(rp)
                    found.append(rp)

    found.sort(key=lambda x: x.name.lower())
    return found


def _extract_datetime_keys(path: Path) -> set[str]:
    """从文件名中提取时次 key。

    兼容：
    - 20260301_0400
    - 20260301_040000
    - 202603010400
    - 20260301040000

    统一生成 YYYYMMDD_HHMM，忽略秒。
    """
    text = path.stem
    keys: set[str] = set()

    for m in re.finditer(r"(?<!\d)(20\d{6})[_-]?(\d{4})(\d{2})?(?!\d)", text):
        date = m.group(1)
        hm = m.group(2)
        keys.add(f"{date}_{hm}")
        keys.add(f"{date}{hm}")

    # 兜底：原始 stem 也放进去，适配不含标准时间的文件名一一对应。
    if not keys:
        keys.add(text.lower())

    return keys


def _build_role_index(files: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for f in files:
        for key in _extract_datetime_keys(f):
            index.setdefault(key, []).append(f)
    return index


def _get_batch_input_fields(module: dict) -> list[dict]:
    fields: list[dict] = []
    for field in module.get("inputs", []) or []:
        if bool(field.get("control_only", False)):
            continue
        role = _field_batch_role(field)
        if role and role.upper() != "OUTPUT_DIR":
            copied = dict(field)
            copied["batch_role"] = role
            fields.append(copied)
    return fields


def _is_batch_request(module: dict, effective_inputs: Dict[str, Any]) -> bool:
    for field in _get_batch_input_fields(module):
        key = field.get("key")
        if not key:
            continue
        value = effective_inputs.get(key)
        if value in ("", None):
            continue
        p = Path(str(value))
        if p.exists() and p.is_dir():
            return True
    return False


def _get_output_dir_field_for_batch(module: dict) -> Optional[dict]:
    for field in module.get("inputs", []) or []:
        if bool(field.get("control_only", False)):
            continue
        if is_output_field(field):
            return field
    return None


def _make_batch_output_value(module: dict, base_inputs: dict, slot: str, primary_file: Path) -> tuple[dict, Optional[Path]]:
    job_inputs = dict(base_inputs)
    output_field = _get_output_dir_field_for_batch(module)
    if not output_field:
        return job_inputs, None

    key = output_field.get("key")
    if not key:
        return job_inputs, None

    raw_value = str(job_inputs.get(key) or "").strip()
    if not raw_value:
        return job_inputs, None

    output_ext = str(output_field.get("output_ext") or output_field.get("suffix") or ".tif")
    if output_ext and not output_ext.startswith("."):
        output_ext = "." + output_ext
    if not output_ext:
        output_ext = ".tif"

    p = Path(raw_value)
    field_type = str(output_field.get("type") or "").lower()

    # 批处理时，输出目录类型默认生成每个 job 一个文件。
    if field_type == "dir_path" or (not p.suffix):
        p.mkdir(parents=True, exist_ok=True)
        output_naming = str(
            (module.get("parallel") or {}).get("output_naming")
            or output_field.get("output_naming")
            or "source_stem"
        ).strip().lower()

        if output_naming in {"source_stem", "input_stem", "primary_stem", "file_stem"}:
            base_name = primary_file.stem
        else:
            base_name = str(slot or primary_file.stem)

        safe_slot = str(base_name).replace(":", "_").replace("/", "_").replace("\\", "_")
        out_path = p / f"{safe_slot}{output_ext}"
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        out_path = p.with_name(f"{p.stem}_{primary_file.stem}{p.suffix}")

    job_inputs[key] = str(out_path.resolve())
    return job_inputs, out_path.resolve()


def _format_batch_validation_error(message: str, missing: list[dict] | None = None, extras: list[dict] | None = None) -> str:
    parts = [message]
    if missing:
        parts.append("缺失匹配：")
        for idx, item in enumerate(missing[:30], start=1):
            parts.append(
                f"{idx}. slot={item.get('slot', '-')} role={item.get('role', '-')} expected_from={item.get('expected_from', '-')}"
            )
        if len(missing) > 30:
            parts.append(f"... 还有 {len(missing) - 30} 项")
    if extras:
        parts.append("未使用文件：")
        for idx, item in enumerate(extras[:20], start=1):
            files = item.get("files") or []
            preview = ", ".join(str(x) for x in files[:5])
            if len(files) > 5:
                preview += f", ... 还有 {len(files) - 5} 个"
            parts.append(f"{idx}. role={item.get('role', '-')} files={preview}")
    return "\n".join(parts)


def build_batch_jobs_for_module(module: dict, inputs: dict, parallel_workers: int) -> tuple[list[dict], list[Path]]:
    batch_fields = _get_batch_input_fields(module)
    if not batch_fields:
        return [], []

    role_files: dict[str, list[Path]] = {}
    role_indexes: dict[str, dict[str, list[Path]]] = {}
    role_fields: dict[str, dict] = {}

    for field in batch_fields:
        key = field.get("key")
        role = _field_batch_role(field) or str(key)
        value = inputs.get(key)
        if value in ("", None):
            continue
        files = _list_batch_files(str(value), field)
        if not files:
            raise HTTPException(status_code=400, detail=f"批量目录为空或没有匹配文件: {key} -> {value}")

        role_files[role] = files
        role_indexes[role] = _build_role_index(files)
        role_fields[role] = field

    if not role_files:
        return [], []

    primary_roles = [
        role for role, field in role_fields.items()
        if not _is_shared_batch_field(field)
    ]
    if not primary_roles:
        primary_roles = list(role_files.keys())

    primary_role = max(primary_roles, key=lambda r: len(role_files.get(r, [])))
    primary_field = role_fields[primary_role]
    primary_key = primary_field.get("key")

    jobs: list[dict] = []
    output_paths: list[Path] = []
    missing: list[dict] = []
    used_by_role: dict[str, set[str]] = {role: set() for role in role_files}

    primary_files = role_files[primary_role]
    total = len(primary_files)

    for idx, primary_path in enumerate(primary_files, start=1):
        keys = _extract_datetime_keys(primary_path)
        slot = sorted(keys)[0] if keys else primary_path.stem
        if "_" not in slot and len(slot) == 12:
            slot = f"{slot[:8]}_{slot[8:12]}"

        job_inputs = dict(inputs)
        job_inputs[primary_key] = str(primary_path.resolve())
        used_by_role[primary_role].add(str(primary_path.resolve()))

        ok = True
        for role, files in role_files.items():
            if role == primary_role:
                continue

            field = role_fields[role]
            key = field.get("key")

            selected: Optional[Path] = None
            # first/shared：取第一个文件给所有 job 共用。
            if _is_shared_batch_field(field):
                selected = files[0]
            # 当前目录只有一个文件时，也允许作为所有 job 共用，方便临时测试 SOLAR。
            elif len(files) == 1:
                selected = files[0]
            else:
                # 正常按时次匹配。
                index = role_indexes[role]
                for k in keys:
                    candidates = index.get(k)
                    if candidates:
                        selected = candidates[0]
                        break

                # 临时兼容：SOLAR 没有对应时次时，允许用排序第一个文件先跑通流程。
                # 正式生产建议把 SOLAR 文件准备成对应时次，或显式配置 match_mode=timeslot。
                if selected is None and str(role).upper() == "SOLAR":
                    selected = files[0]

            if selected is None:
                ok = False
                missing.append({
                    "slot": slot,
                    "role": role,
                    "expected_from": str(primary_path),
                })
                continue

            job_inputs[key] = str(selected.resolve())
            used_by_role.setdefault(role, set()).add(str(selected.resolve()))

        if not ok:
            continue

        job_inputs, out_path = _make_batch_output_value(module, job_inputs, slot, primary_path)
        if out_path is not None:
            output_paths.append(out_path)

        # 平台字段不写进 exe config。
        for field in module.get("inputs", []) or []:
            if field.get("control_only") is True:
                k = field.get("key")
                if k:
                    job_inputs.pop(k, None)

        job_inputs["_batch_index"] = idx
        job_inputs["_batch_total"] = total
        job_inputs["_batch_slot"] = slot

        # 不把平台内部字段写给 exe，除非模块显式要求。
        exe_inputs = {
            k: v for k, v in job_inputs.items()
            if not str(k).startswith("_batch_")
        }

        command, working_dir, runtime_env = build_runtime_for_module(module, exe_inputs)
        jobs.append({
            "module_id": module.get("id", ""),
            "module_name": module.get("name", module.get("id", "")),
            "label": f"{idx}/{total} {slot}",
            "command": command,
            "working_dir": working_dir,
            "env": runtime_env,
            "inputs": exe_inputs,
        })

    if missing:
        extras: list[dict] = []
        for role, files in role_files.items():
            unused = [str(f) for f in files if str(f.resolve()) not in used_by_role.get(role, set())]
            if unused:
                extras.append({"role": role, "files": unused})
        raise HTTPException(
            status_code=400,
            detail=_format_batch_validation_error(
                "批量输入不匹配，请检查各输入目录文件名时次是否对应。"
                " 如果只是临时测试 SOLAR，可以把 SOLAR_file 的 match_mode 改成 first；"
                "本版也会在 SOLAR 找不到时次时自动取第一个文件兜底。",
                missing,
                extras,
            ),
        )

    if not jobs:
        raise HTTPException(status_code=400, detail="没有生成任何批处理 job，请检查输入目录和 batch_role 配置")

    return jobs, output_paths
def task_belongs_to_user(task: dict, user) -> bool:
    if not task:
        return False

    owner = str(task.get("owner_username") or "")
    username = get_username_from_user(user)

    return bool(owner) and owner == username


def require_own_task(task_id: str, user) -> dict:
    task = task_manager.get_task(task_id)
    if not task or not task_belongs_to_user(task, user):
        raise HTTPException(status_code=404, detail="任务不存在")
    return task

# =========================
# 任务接口
# =========================
@app.get("/api/tasks")
def api_list_tasks(authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    username = get_username_from_user(user)
    return task_manager.list_tasks(owner_username=username)


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: str, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    return require_own_task(task_id, user)


@app.post("/api/tasks/{task_id}/cancel")
def api_cancel_task(task_id: str, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    require_own_task(task_id, user)

    ok = task_manager.cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: str, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    require_own_task(task_id, user)

    ok = task_manager.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}

def field_io_role(field: dict) -> str:
    """读取模块 JSON 中用于区分输入/输出的显式字段。

    推荐在 inputs 的每一项里加：
      "io_role": "input"   # 输入文件/输入目录/管理员固定资源
      "io_role": "output"  # 输出文件/输出目录

    兼容别名：
      data_role / file_role / direction / role
    没有显式配置时返回 auto，再走旧的 key/label 关键词判断，兼容老模块。
    """
    for name in ("io_role", "data_role", "file_role", "direction", "role"):
        value = field.get(name)
        if value in (None, ""):
            continue
        text = str(value).strip().lower()
        if text in {"output", "out", "result", "result_file", "result_dir", "save", "输出", "结果"}:
            return "output"
        if text in {"input", "in", "source", "source_file", "source_dir", "resource", "输入", "源文件", "资源"}:
            return "input"
        if text in {"auto", "none", "unknown"}:
            return "auto"
    return "auto"


def is_output_field(field: dict) -> bool:
    # 显式 io_role 优先。标成 input 的字段，即使 key/label 里有特殊词，也不会被登记到数据管理。
    role = field_io_role(field)
    if role == "output":
        return True
    if role == "input":
        return False

    key = str(field.get("key", "")).lower()
    label = str(field.get("label", "")).lower()

    output_keywords = [
        "output",
        "outpath",
        "out_dir",
        "output_dir",
        "result",
        "save",
        "输出",
        "结果",
    ]

    return any(k in key or k in label for k in output_keywords)
def ensure_data_files_file():
    if not DATA_FILES_FILE.exists():
        DATA_FILES_FILE.write_text("[]", encoding="utf-8")


def load_data_files() -> list[dict]:
    ensure_data_files_file()
    try:
        data = json.loads(DATA_FILES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_data_files(items: list[dict]):
    DATA_FILES_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_file_type(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return suffix or "unknown"


def format_file_size(size: int) -> str:
    size = int(size or 0)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.2f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.2f} MB"
    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def collect_output_paths_from_inputs(module: dict, inputs: dict) -> list[Path]:
    """
    从模块输入参数里找出输出路径。
    只记录路径，不移动文件。
    """
    paths: list[Path] = []

    for field in module.get("inputs", []) or []:
        key = field.get("key")
        if not key:
            continue

        if not is_output_field(field):
            continue

        value = str(inputs.get(key) or "").strip()
        if not value:
            continue

        paths.append(Path(value))

    return paths


def scan_output_files(output_paths: list[Path]) -> list[Path]:
    """
    扫描输出路径下的真实文件。
    - 如果输出路径是文件：记录这个文件
    - 如果输出路径是文件夹：递归记录文件夹下的文件
    """
    files: list[Path] = []
    seen = set()

    for raw_path in output_paths:
        p = Path(raw_path)

        if p.is_file():
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                files.append(rp)

        elif p.is_dir():
            for item in p.rglob("*"):
                if item.is_file():
                    rp = item.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        files.append(rp)

    files.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
    return files


def upsert_data_files_from_outputs(
    module: dict,
    task_id: str,
    output_paths: list[Path],
    owner_username: str = "",
):
    """
    把任务输出结果登记到 data_files.json。
    只登记信息，不移动文件。
    """
    existing = load_data_files()
    by_key = {}

    for item in load_data_files():
        if not isinstance(item, dict):
            continue
        path_text = str(item.get("path") or "")
        owner = str(item.get("owner_username") or "")
        key = f"{owner}::{path_text}"
        by_key[key] = item

    files = scan_output_files(output_paths)

    for file_path in files:
        if not file_path.exists() or not file_path.is_file():
            continue

        stat = file_path.stat()
        path_text = str(file_path.resolve())

        record_key = f"{owner_username}::{path_text}"
        old = by_key.get(record_key) or {}

        by_key[record_key] = {
            **old,
            "path": path_text,
            "name": file_path.name,
            "file_name": file_path.name,
            "file_type": get_file_type(file_path),
            "io_role": "output",
            "data_role": "output",
            "source_kind": "module_output",
            "module_id": module.get("id", ""),
            "module_name": module.get("name") or module.get("id", ""),
            "task_id": task_id,
            "owner_username": str(owner_username or ""),
            "size": stat.st_size,
            "size_text": format_file_size(stat.st_size),
            "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="seconds"),
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        }

    items = list(by_key.values())
    items.sort(key=lambda x: x.get("modified_at", ""), reverse=True)

    for idx, item in enumerate(items):
        item["id"] = idx

    save_data_files(items)
    return items


def start_data_file_scan_after_task(
    task_id: str,
    module: dict,
    output_paths: list[Path],
    owner_username: str = "",
):
    """
    任务结束后扫描输出路径，将结果登记到数据管理。
    """
    import threading
    import time

    terminal_statuses = {"success", "failed", "cancelled"}

    def worker():
        while True:
            task = task_manager.get_task(task_id)
            if not task:
                return

            status = task.get("status")
            if status in terminal_statuses:
                if status == "success":
                    try:
                        task_owner = owner_username or str(task.get("owner_username") or "")

                        upsert_data_files_from_outputs(
                            module=module,
                            task_id=task_id,
                            output_paths=output_paths,
                            owner_username=task_owner,
                        )
                        try:
                            task_manager.append_log(task_id, "[DATA] 输出结果已登记到数据管理")
                        except Exception:
                            pass
                    except Exception as exc:
                        try:
                            task_manager.append_log(task_id, f"[DATA-ERROR] 数据管理登记失败: {repr(exc)}")
                        except Exception:
                            pass
                else:
                    try:
                        task_manager.append_log(task_id, f"[DATA] 任务状态为 {status}，不登记输出文件")
                    except Exception:
                        pass
                return

            time.sleep(2)

    threading.Thread(target=worker, daemon=True).start()

# =========================
# 数据管理接口
# =========================
@app.get("/api/data/files")
def api_list_data_files(authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    username = get_username_from_user(user)
    if not username:
        raise HTTPException(status_code=401, detail="未登录")

    # 管理员查看全部用户输出文件
    if isinstance(user, dict):
        role = str(user.get("role") or "")
    else:
        role = str(getattr(user, "role", "") or "")

    all_items, visible_items = load_visible_data_files_for_user(username)

    if role == "admin":
        result = [dict(item) for item in all_items]
    else:
        result = [dict(item) for item in visible_items]

    for item in result:
        item.pop("_source_index", None)

    return result


@app.post("/api/data/files/{file_id}/reveal")
def api_reveal_data_file(file_id: int, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    username = get_username_from_user(user)
    if not username:
        raise HTTPException(status_code=401, detail="未登录")

    _, _, item = get_data_file_by_id_with_permission(file_id, user)

    path = Path(str(item.get("path") or ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    folder = path.parent

    try:
        if os.name == "nt":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打开文件所在位置失败: {exc}")

    return {"ok": True}

@app.delete("/api/data/files/{file_id}")
def api_delete_data_file(file_id: int, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    username = get_username_from_user(user)
    if not username:
        raise HTTPException(status_code=401, detail="未登录")

    # 只删除 data_files.json 中的登记记录，不删除本地真实文件
    items, source_index, item = get_data_file_by_id_with_permission(file_id, user)

    items.pop(source_index)

    for idx, row in enumerate(items):
        row["id"] = idx

    save_data_files(items)
    return {"ok": True, "message": "已从数据管理列表移除，本地文件未删除"}


@app.get("/api/data/files/{file_id}/preview")
def api_preview_data_file(file_id: int, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    username = get_username_from_user(user)
    if not username:
        raise HTTPException(status_code=401, detail="未登录")

    _, _, item = get_data_file_by_id_with_permission(file_id, user)

    path = Path(str(item.get("path") or ""))

    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    suffix = path.suffix.lower()

    if suffix in {".tif", ".tiff"}:
        result = render_tif_to_preview_result(path)
        return {
            "type": "image",
            "name": path.name,
            "path": str(path.resolve()),
            "data_url": _png_data_url(result["png"]),
            "meta": result.get("meta", {}),
        }

    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}:
        data = path.read_bytes()
        mime = "image/png"
        if suffix in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif suffix == ".gif":
            mime = "image/gif"
        elif suffix == ".webp":
            mime = "image/webp"
        encoded = base64.b64encode(data).decode("ascii")
        return {
            "type": "image",
            "name": path.name,
            "path": str(path.resolve()),
            "data_url": f"data:{mime};base64,{encoded}",
            "meta": {},
        }

    return {
        "type": "file",
        "name": path.name,
        "path": str(path.resolve()),
        "message": "该文件类型暂不支持在线预览",
    }
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

