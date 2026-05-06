from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
import uuid
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
# 通用辅助函数
VALID_PARALLEL_MODES = {"none", "auto", "single_file", "folder_chunks", "module_internal"}
DEFAULT_PARALLEL_PATTERNS = "*.tif;*.tiff;*.nc;*.hdf;*.h5"

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


def render_tif_to_preview_result(tif_path: Path) -> dict:
    """把 tif/tiff 渲染成浏览器可显示的 PNG，同时返回拉伸统计信息。"""
    if not tif_path.exists() or not tif_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if not is_tif_path(tif_path):
        raise HTTPException(status_code=400, detail="只支持预览 tif/tiff 文件")

    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"缺少图片预览依赖 Pillow/numpy: {exc}")

    max_size = 1600
    meta: dict[str, Any] = {"name": tif_path.name, "size": tif_path.stat().st_size, "suffix": tif_path.suffix.lower()}

    try:
        from osgeo import gdal

        ds = gdal.Open(str(tif_path))
        if ds is None:
            raise RuntimeError("GDAL 无法打开该 tif")

        width = int(ds.RasterXSize)
        height = int(ds.RasterYSize)
        scale = min(1.0, max_size / max(width, height)) if max(width, height) else 1.0
        out_w = max(1, int(width * scale))
        out_h = max(1, int(height * scale))

        meta.update({"width": width, "height": height, "bands": int(ds.RasterCount or 1), "preview_width": out_w, "preview_height": out_h, "preview_engine": "python_osgeo_gdal"})

        band_count = int(ds.RasterCount or 1)
        if band_count >= 3:
            bands = []
            band_stats = []
            for band_index in (1, 2, 3):
                band = ds.GetRasterBand(band_index)
                nodata = band.GetNoDataValue()
                arr = band.ReadAsArray(buf_xsize=out_w, buf_ysize=out_h)
                band_stats.append({"band": band_index, **_array_preview_stats(arr, nodata=nodata)})
                bands.append(_normalize_to_uint8(arr, nodata=nodata, prefer_nonzero=True))
            rgb = np.dstack(bands)
            image = Image.fromarray(rgb, mode="RGB")
            meta["band_stats"] = band_stats
            meta["render_mode"] = "rgb_stretched"
        else:
            band = ds.GetRasterBand(1)
            nodata = band.GetNoDataValue()
            arr = band.ReadAsArray(buf_xsize=out_w, buf_ysize=out_h)
            gray = _normalize_to_uint8(arr, nodata=nodata, prefer_nonzero=True)
            image = _colorize_gray(gray)
            meta.update(_array_preview_stats(arr, nodata=nodata))
            meta["render_mode"] = "single_band_contrast_colorized"
    except HTTPException:
        raise
    except Exception as gdal_exc:
        # 关键修复：Python 没有 osgeo，或 osgeo/GDAL 打不开时，不再直接依赖 Pillow。
        # 优先调用系统 gdal_translate；你的电脑上 gdalinfo 能打开 AnnualCrop_1.tif，
        # 因此这个分支可以处理 Pillow 无法识别的多波段 GeoTIFF。
        try:
            cli_meta = dict(meta)
            cli_meta["python_gdal_error"] = str(gdal_exc)
            return _render_tif_with_gdal_cli(tif_path, cli_meta)
        except HTTPException:
            raise
        except Exception as cli_exc:
            try:
                image = Image.open(tif_path)
                image.thumbnail((max_size, max_size))
                if image.mode not in {"L", "RGB", "RGBA"}:
                    image = image.convert("RGB")
                meta["render_mode"] = "pillow_fallback"
                meta["preview_engine"] = "pillow"
                meta["width"], meta["height"] = image.size
                meta["python_gdal_error"] = str(gdal_exc)
                meta["gdal_cli_error"] = str(cli_exc)
            except Exception as pil_exc:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "tif 预览失败：Python 后端没有可用的 osgeo/GDAL，"
                        "系统 gdal_translate 调用也失败，Pillow 也无法识别该 GeoTIFF。"
                        f" Python GDAL 错误: {gdal_exc}; GDAL CLI 错误: {cli_exc}; Pillow 错误: {pil_exc}"
                    ),
                )

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return {"png": buf.getvalue(), "meta": meta}


def render_tif_to_png_bytes(tif_path: Path) -> bytes:
    return render_tif_to_preview_result(tif_path)["png"]


def is_previewable_path(path: Path) -> bool:
    return path.suffix.lower() in {".tif", ".tiff", ".nc", ".nc4", ".cdf", ".hdf", ".h5"}


def _png_data_url(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_nc_to_preview(nc_path: Path) -> dict:
    """把上传的 nc/hdf/h5 文件预览为图片；如果没有可绘制变量，就返回元数据。"""
    if not nc_path.exists() or not nc_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"缺少预览依赖 Pillow/numpy: {exc}")

    meta: dict[str, Any] = {
        "name": nc_path.name,
        "size": nc_path.stat().st_size,
        "suffix": nc_path.suffix.lower(),
    }

    try:
        import xarray as xr

        ds = xr.open_dataset(nc_path, mask_and_scale=True)
        meta["dimensions"] = {str(k): int(v) for k, v in ds.sizes.items()}
        meta["variables"] = [str(k) for k in ds.data_vars.keys()]
        meta["coords"] = [str(k) for k in ds.coords.keys()]

        selected_name = ""
        selected = None
        for name, da in ds.data_vars.items():
            try:
                if da.ndim >= 2 and np.issubdtype(da.dtype, np.number):
                    selected_name = str(name)
                    selected = da
                    break
            except Exception:
                continue

        if selected is None:
            try:
                ds.close()
            except Exception:
                pass
            return {
                "kind": "metadata",
                "title": nc_path.name,
                "message": "该文件没有找到可直接绘图的二维数值变量，下面显示文件结构。",
                "meta": meta,
            }

        while selected.ndim > 2:
            selected = selected.isel({selected.dims[0]: 0})

        arr = np.asarray(selected.values)
        arr = np.squeeze(arr)
        if arr.ndim != 2:
            try:
                ds.close()
            except Exception:
                pass
            return {
                "kind": "metadata",
                "title": nc_path.name,
                "message": f"变量 {selected_name} 不能转为二维图像，下面显示文件结构。",
                "meta": meta,
            }

        max_size = 1600
        h, w = arr.shape
        scale = min(1.0, max_size / max(h, w)) if max(h, w) else 1.0
        gray = _normalize_to_uint8(arr, nodata=None, prefer_nonzero=True)
        image = _colorize_gray(gray)
        if scale < 1.0:
            image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))))

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        try:
            ds.close()
        except Exception:
            pass

        meta["preview_variable"] = selected_name
        meta["preview_shape"] = list(arr.shape)
        meta["render_mode"] = "nc_single_variable_contrast_colorized"
        meta["preview_stats"] = _array_preview_stats(arr, nodata=None)
        return {
            "kind": "image",
            "title": nc_path.name,
            "image_data_url": _png_data_url(buf.getvalue()),
            "meta": meta,
            "message": f"已预览变量：{selected_name}（多维数据默认取第一个切片）。",
        }
    except Exception as exc:
        return {
            "kind": "metadata",
            "title": nc_path.name,
            "message": f"无法生成图像预览：{exc}",
            "meta": meta,
        }


def build_uploaded_file_preview(target: Path) -> dict:
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if not is_previewable_path(target):
        raise HTTPException(status_code=400, detail="当前仅支持预览 tif/tiff/nc/nc4/cdf/hdf/h5 文件")

    suffix = target.suffix.lower()
    if suffix in {".tif", ".tiff"}:
        result = render_tif_to_preview_result(target)
        zero_ratio = result.get("meta", {}).get("zero_ratio")
        extra = ""
        if isinstance(zero_ratio, (int, float)) and zero_ratio >= 0.5:
            extra = " 已自动排除大量 0 背景后做对比度拉伸。"
        return {
            "kind": "image",
            "title": target.name,
            "image_data_url": _png_data_url(result["png"]),
            "meta": result.get("meta", {"name": target.name, "size": target.stat().st_size, "suffix": suffix}),
            "message": "已将上传的 GeoTIFF 转为浏览器可显示的 PNG 预览。" + extra,
        }

    return render_nc_to_preview(target)

def get_user_upload_dirs(username: str):
    user_dir = UPLOADS_DIR / username
    input_dir = user_dir / "输入文件夹"
    output_dir = user_dir / "输出文件夹"

    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    return user_dir, input_dir, output_dir
@app.get("/api/files")
def api_list_user_files(authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    user_dir, input_dir, output_dir = get_user_upload_dirs(user.username)

    items = []
    for p in sorted(user_dir.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        stat = p.stat()
        items.append({
            "name": p.name,
            "path": str(p.resolve()),
            "relative_path": str(p.relative_to(user_dir)),
            "size": stat.st_size if p.is_file() else 0,
            "type": "file" if p.is_file() else "dir",
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })

    return items


@app.post("/api/files/upload")
def api_upload_user_file(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    user = get_current_user(authorization)
    user_dir, input_dir, output_dir = get_user_upload_dirs(user.username)

    original_name = sanitize_filename(file.filename or "uploaded_file")

    allowed_suffixes = {".tif", ".tiff", ".nc", ".nc4", ".cdf", ".hdf", ".h5"}
    if Path(original_name).suffix.lower() not in allowed_suffixes:
        raise HTTPException(
            status_code=400,
            detail="文件格式不支持，仅支持 tif、tiff、nc、nc4、cdf、hdf、h5 文件",
        )

    target = input_dir / original_name

    # 如重名，自动追加编号
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        idx = 1
        while True:
            candidate = input_dir / f"{stem}_{idx}{suffix}"
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
        "relative_path": str(target.relative_to(user_dir)),
        "size": target.stat().st_size,
    }

@app.post("/api/tasks/run")
def api_run_module(payload: ModuleRunRequest, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)

    module = get_module(payload.module_id)
    if not module:
        raise HTTPException(status_code=404, detail="模块不存在")
    if not module.get("enabled", True):
        raise HTTPException(status_code=400, detail="模块已禁用")

    inputs = merge_admin_fixed_inputs(module, payload.inputs or {})
    parallel_workers = clamp_parallel_workers(payload.parallel_workers)

    # 先检查必填参数；输出字段由后端自动生成，所以不要求用户填写
    for field in module.get("inputs", []):
        key = field.get("key")
        required = field.get("required", False)

        if is_output_field(field):
            continue

        if required and (key not in inputs or inputs.get(key) in ("", None)):
            raise HTTPException(status_code=400, detail=f"缺少必填参数: {key}")

    # 整理用户输入/输出目录：
    # 输入文件/文件夹复制到 uploads/用户名/输入文件夹
    # 输出目录保留用户选择的原始目录，任务结束后同步到 uploads/用户名/输出文件夹/输出目录名
    inputs, job_output_dir, output_syncs = normalize_task_io_paths(
        module=module,
        inputs=inputs,
        username=user.username,
    )
    # control_only 字段只用于平台控制，不写入模块 config.json
    for field in module.get("inputs", []) or []:
        if field.get("control_only") is True:
            inputs.pop(field.get("key"), None)

    mode = str(normalize_parallel_config(module).get("mode") or "auto").strip() or "auto"

    if parallel_workers > 1 and mode == "module_internal":
        # 模块源码自己处理并行，平台只负责传参，不拆成多个进程。
        inputs = dict(inputs)
        inputs["parallel_workers"] = parallel_workers
        inputs["_parallel_workers"] = parallel_workers

    # 旧系统进程池逻辑：模块输入字段只要显式配置 batch_role，就按文件匹配生成多个 job，
    # 再由 TaskManager.submit_batch_group 用 ThreadPoolExecutor 控制并发。
    if _is_batch_request(module, inputs):
        batch_result = _run_batch_internal(module, inputs, parallel_workers)
        parent_task = batch_result.get("parent_task") if isinstance(batch_result, dict) else None
        if output_syncs and isinstance(parent_task, dict) and parent_task.get("id"):
            start_output_sync_after_task(parent_task["id"], output_syncs)
        if isinstance(parent_task, dict):
            return parent_task
        return batch_result

    jobs = prepare_parallel_jobs(module, inputs, parallel_workers)

    if parallel_workers > 1 and len(jobs) > 1:
        task = task_manager.submit_parallel_module_task(
            module_id=module["id"],
            module_name=module.get("name", module["id"]),
            jobs=jobs,
            inputs={
                **inputs,
                "parallel_workers": parallel_workers,
                "_user_output_dir": str(job_output_dir.resolve()),
            },
            max_workers=parallel_workers,
        )

        if output_syncs:
            start_output_sync_after_task(task["id"], output_syncs)

        return task

    command, working_dir, runtime_env = build_runtime_for_module(module, inputs)

    task = task_manager.submit_module_task(
        module_id=module["id"],
        module_name=module.get("name", module["id"]),
        command=command,
        inputs=inputs,
        working_dir=working_dir,
        env=runtime_env,
    )

    if output_syncs:
        start_output_sync_after_task(task["id"], output_syncs)

    return task
@app.delete("/api/files/{filename}")
def api_delete_user_file(filename: str, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    user_dir, input_dir, output_dir = get_user_upload_dirs(user.username)

    safe_name = sanitize_filename(filename)

    matches = [p for p in user_dir.rglob(safe_name) if p.is_file()]
    if not matches:
        raise HTTPException(status_code=404, detail="文件不存在")

    target = matches[0]
    target.unlink()

    return {"ok": True}


@app.get("/api/files/{filename}/download")
def api_download_user_file(filename: str, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    user_dir, input_dir, output_dir = get_user_upload_dirs(user.username)

    safe_name = sanitize_filename(filename)
    matches = [p for p in user_dir.rglob(safe_name) if p.is_file()]

    if not matches:
        raise HTTPException(status_code=404, detail="文件不存在")

    target = matches[0]
    return FileResponse(str(target), filename=target.name)


@app.get("/api/files/{filename}/preview")
def api_preview_uploaded_file(filename: str, authorization: str | None = Header(default=None)):
    user = get_current_user(authorization)
    user_dir, input_dir, output_dir = get_user_upload_dirs(user.username)
    safe_name = sanitize_filename(filename)
    matches = [p for p in user_dir.rglob(safe_name) if p.is_file()]
    if not matches:
        raise HTTPException(status_code=404, detail="文件不存在")

    target = matches[0]
    return build_uploaded_file_preview(target)

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


def resolve_module_dir(module: dict) -> Path:
    working_dir = module.get("working_dir", ".")
    project_root = BASE_DIR.parent
    module_dir = Path(working_dir)
    if not module_dir.is_absolute():
        module_dir = (project_root / module_dir).resolve()
    else:
        module_dir = module_dir.resolve()
    return module_dir


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
# 旧系统进程池批处理逻辑：batch_role 字段 -> 多个 job -> ThreadPoolExecutor 控制并发
# =========================
BATCH_FILE_EXTS = {".tif", ".tiff", ".img", ".hdf", ".h5", ".nc", ".nc4", ".dat", ".json"}


def _strip_control_only_inputs(module: dict, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """去掉只给平台调度用的字段，避免写进 exe 的 config.json。"""
    result = dict(inputs or {})
    for field in module.get("inputs", []) or []:
        if bool(field.get("control_only", False)):
            key = field.get("key")
            if key:
                result.pop(key, None)
    # 平台内部字段也不写入模块 config.json。
    for key in list(result.keys()):
        if str(key).startswith("_"):
            result.pop(key, None)
    return result


def _validate_required_inputs(module: dict, inputs: Dict[str, Any], skip_control_only: bool = True):
    for field in module.get("inputs", []) or []:
        key = field.get("key")
        if not key:
            continue
        if skip_control_only and bool(field.get("control_only", False)):
            continue
        if is_output_field(field):
            continue
        if field.get("required", False) and inputs.get(key) in ("", None):
            raise HTTPException(status_code=400, detail=f"缺少必填参数: {key}")


def _is_probably_batch_field(field: dict, value: Any) -> bool:
    """只把显式配置 batch_role 的输入字段当作批处理输入。"""
    if not value:
        return False
    if bool(field.get("control_only", False)):
        return False
    role = str(field.get("batch_role") or "").strip()
    if not role or role.upper() == "OUTPUT_DIR":
        return False
    return True


def _get_batch_input_fields(module: dict) -> List[dict]:
    fields = []
    for field in module.get("inputs", []) or []:
        if bool(field.get("control_only", False)):
            continue
        role = str(field.get("batch_role") or "").strip()
        if role and role.upper() != "OUTPUT_DIR":
            fields.append(field)
    return fields


def _get_output_dir_field(module: dict) -> Optional[dict]:
    for field in module.get("inputs", []) or []:
        key = str(field.get("key", "")).lower()
        label = str(field.get("label", ""))
        field_type = field.get("type")
        role = str(field.get("batch_role") or "").strip().upper()
        if field_type not in {"dir_path", "file_path"}:
            continue
        if role == "OUTPUT_DIR":
            return field
        if key in {"output", "out", "output_dir", "output_path", "result_dir"}:
            return field
        if "输出" in label:
            return field
    return None


def _list_data_files(dir_path: str, field: dict | None = None) -> List[Path]:
    p = Path(str(dir_path))
    if not p.exists() or not p.is_dir():
        return []
    field = field or {}
    include_regex = str(field.get("batch_include_regex") or "").strip()
    exclude_regex = str(field.get("batch_exclude_regex") or "").strip()
    include_re = re.compile(include_regex, re.IGNORECASE) if include_regex else None
    exclude_re = re.compile(exclude_regex, re.IGNORECASE) if exclude_regex else None
    items: List[Path] = []
    for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
        if not child.is_file():
            continue
        name = child.name
        if name.startswith("."):
            continue
        lower_name = name.lower()
        if lower_name.endswith((".tmp", ".bak", ".log", ".txt", ".xml")):
            continue
        if child.suffix and child.suffix.lower() not in BATCH_FILE_EXTS:
            continue
        if include_re and not include_re.search(name):
            continue
        if exclude_re and exclude_re.search(name):
            continue
        items.append(child.resolve())
    return items


def _extract_datetime_keys(stem: str) -> List[str]:
    keys: List[str] = []
    m = re.search(r"(20\d{6})[_-]?(\d{6})", stem)
    if m:
        d = m.group(1)
        t = m.group(2)
        keys.append(f"{d}_{t}")
        if t.endswith("00"):
            keys.append(f"{d}_{t[:4]}")
    m2 = re.search(r"(20\d{6})[_-]?(\d{4})", stem)
    if m2:
        keys.append(f"{m2.group(1)}_{m2.group(2)}")
    return list(dict.fromkeys(keys))


def _normalize_stem_token(stem: str, role: str, field_key: str) -> str:
    s = stem.upper()
    replacements = [
        role.upper(), field_key.upper(),
        "B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08",
        "SOLAR", "SUN", "OUTPUT", "OUT", "FLDK", "R10", "R20", "R05",
        "TA", "CN", "AHI", "HS", "H09", "H08",
    ]
    for token in replacements:
        s = re.sub(rf"(^|[_\-.]){re.escape(token)}(?=$|[_\-.])", "_", s)
    s = re.sub(r"20\d{6}[_-]?\d{6}", "_", s)
    s = re.sub(r"20\d{6}[_-]?\d{4}", "_", s)
    s = re.sub(r"[^A-Z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _candidate_keys_for_file(path: Path, role: str, field_key: str) -> List[str]:
    stem = path.stem
    keys = _extract_datetime_keys(stem)
    normalized = _normalize_stem_token(stem, role, field_key)
    if normalized:
        keys.append(normalized)
    if not keys:
        keys.append(stem.upper())
    return list(dict.fromkeys(keys))


def _build_role_index(files: List[Path], role: str, field_key: str) -> Dict[str, List[Path]]:
    index: Dict[str, List[Path]] = {}
    for f in files:
        for key in _candidate_keys_for_file(f, role, field_key):
            index.setdefault(key, []).append(f)
    return index


def _pick_match_for_slot(index: Dict[str, List[Path]], slot: str, used: set[str]) -> Optional[Path]:
    for item in index.get(slot, []):
        if str(item) not in used:
            return item
    return None


def _is_batch_request(module: dict, effective_inputs: Dict[str, Any]) -> bool:
    for field in module.get("inputs", []) or []:
        key = field.get("key")
        if not key:
            continue
        if _is_probably_batch_field(field, effective_inputs.get(key)):
            return True
    return False


def _validate_batch_inputs(module: dict, effective_inputs: Dict[str, Any]) -> Dict[str, Any]:
    batch_fields = _get_batch_input_fields(module)
    if not batch_fields:
        return {"batch_mode": False, "matched_jobs": [], "missing": [], "extras": []}

    role_file_lists: Dict[str, List[Path]] = {}
    role_indexes: Dict[str, Dict[str, List[Path]]] = {}
    used_paths_by_role: Dict[str, set[str]] = {}

    for field in batch_fields:
        key = field["key"]
        role = field.get("batch_role") or key
        dir_value = effective_inputs.get(key, "")
        files = _list_data_files(str(dir_value), field)
        if not files:
            raise HTTPException(status_code=400, detail=f"批量目录为空或不存在: {key} -> {dir_value}")
        role_file_lists[role] = files
        role_indexes[role] = _build_role_index(files, role, key)
        used_paths_by_role[role] = set()

    primary_role = max(role_file_lists.keys(), key=lambda r: len(role_file_lists[r]))
    primary_field = next(f for f in batch_fields if (f.get("batch_role") or f["key"]) == primary_role)

    matched_jobs = []
    missing = []

    for primary_file in role_file_lists[primary_role]:
        slot_candidates = _candidate_keys_for_file(primary_file, primary_role, primary_field["key"])
        slot = slot_candidates[0]
        role_files: Dict[str, str] = {primary_role: str(primary_file)}
        used_paths_by_role[primary_role].add(str(primary_file))
        local_missing = []

        for field in batch_fields:
            role = field.get("batch_role") or field["key"]
            if role == primary_role:
                continue
            files = role_file_lists[role]
            if len(files) == 1:
                role_files[role] = str(files[0])
                used_paths_by_role[role].add(str(files[0]))
                continue
            matched_path = None
            for candidate in slot_candidates:
                matched_path = _pick_match_for_slot(role_indexes[role], candidate, used_paths_by_role[role])
                if matched_path:
                    break
            if matched_path is None:
                local_missing.append({"slot": slot, "role": role, "expected_from": str(primary_file)})
            else:
                role_files[role] = str(matched_path)
                used_paths_by_role[role].add(str(matched_path))

        if local_missing:
            missing.extend(local_missing)
            continue

        output_dir = ""
        output_field = _get_output_dir_field(module)
        if output_field:
            output_dir = str(effective_inputs.get(output_field["key"], "")).strip()

        matched_jobs.append({"slot": slot, "primary_file": str(primary_file), "role_files": role_files, "output_dir": output_dir})

    extras = []
    for role, files in role_file_lists.items():
        unused = [str(f) for f in files if str(f) not in used_paths_by_role[role]]
        if unused:
            extras.append({"role": role, "files": unused})

    return {"batch_mode": True, "primary_role": primary_role, "matched_jobs": matched_jobs, "missing": missing, "extras": extras}


def _resolve_admin_fixed_for_job(module: dict, job_inputs: Dict[str, Any]) -> Dict[str, Any]:
    """确保管理员固定输入/隐藏输入在每个子任务中都是有效路径。"""
    resolved = dict(job_inputs)
    for field in module.get("inputs", []) or []:
        key = field.get("key")
        if not key:
            continue
        visible = field.get("visible_to_user", True) is not False
        admin_fixed = bool(field.get("admin_fixed", False)) or not visible or field.get("path_mode") == "relative_to_module"
        if not admin_fixed:
            continue
        value = resolved.get(key)
        if value in ("", None):
            value = field.get("default")
        if value in ("", None):
            continue
        value = resolve_input_value_for_module(module, field, value)
        if field.get("type") in {"file_path", "dir_path"}:
            p = Path(str(value))
            if not p.exists():
                raise HTTPException(status_code=400, detail=f"管理员固定输入文件不存在: {key} -> {p}")
        resolved[key] = value
    return resolved


def _build_single_job_inputs_from_batch(module: dict, base_inputs: Dict[str, Any], role_files: Dict[str, str], slot: str, output_dir: str) -> Dict[str, Any]:
    """把一组匹配文件转换成一个 exe 的 config 输入。"""
    job_inputs = dict(base_inputs)

    for field in module.get("inputs", []) or []:
        key = field.get("key")
        role = str(field.get("batch_role") or "").strip()
        if key and role and role.upper() != "OUTPUT_DIR" and role in role_files:
            job_inputs[key] = role_files[role]

    output_field = _get_output_dir_field(module)
    if output_field:
        key = output_field["key"]
        ext = output_field.get("output_ext") or ".tif"
        if not str(ext).startswith("."):
            ext = "." + str(ext)
        safe_slot = re.sub(r"[^A-Za-z0-9_\-.]+", "_", slot)
        out_value = output_dir or job_inputs.get(key) or str(RUNTIME_DIR / "outputs")
        out_path = Path(str(out_value))
        # 如果模块把输出字段定义成 file_path，或用户填的是具体文件名，则使用其父目录批量生成文件。
        if output_field.get("type") == "file_path" or out_path.suffix:
            out_dir = out_path.parent
        else:
            out_dir = out_path
        out_dir.mkdir(parents=True, exist_ok=True)
        # 旧系统进程池逻辑：输出字段在单个 job 里改成最终输出文件路径。
        job_inputs[key] = str((out_dir / f"{safe_slot}{ext}").resolve())

    job_inputs = _resolve_admin_fixed_for_job(module, job_inputs)
    job_inputs = _strip_control_only_inputs(module, job_inputs)

    for field in _get_batch_input_fields(module):
        key = field.get("key")
        if not key or key not in job_inputs:
            continue
        value = Path(str(job_inputs[key]))
        if value.exists() and value.is_dir():
            raise HTTPException(status_code=400, detail=f"批处理字段没有被拆成单文件: {key} -> {job_inputs[key]}")

    return job_inputs



def _format_batch_validation_error(validation: Dict[str, Any]) -> str:
    parts = ["批量输入不匹配，请检查各输入目录中的文件名时次是否能对应。"]
    missing = validation.get("missing") or []
    extras = validation.get("extras") or []

    if missing:
        parts.append("缺失匹配：")
        for idx, item in enumerate(missing[:30], start=1):
            if isinstance(item, dict):
                parts.append(
                    f"{idx}. slot={item.get('slot', '-')} role={item.get('role', '-')} expected_from={item.get('expected_from', '-')}"
                )
            else:
                parts.append(f"{idx}. {item}")
        if len(missing) > 30:
            parts.append(f"... 还有 {len(missing) - 30} 项缺失匹配")

    if extras:
        parts.append("未使用文件：")
        for idx, item in enumerate(extras[:20], start=1):
            if isinstance(item, dict):
                files = item.get("files") or []
                preview = ", ".join(str(x) for x in files[:5])
                if len(files) > 5:
                    preview += f", ... 还有 {len(files) - 5} 个"
                parts.append(f"{idx}. role={item.get('role', '-')} files={preview}")
            else:
                parts.append(f"{idx}. {item}")

    return "\n".join(parts)


def _run_batch_internal(module: dict, effective_inputs: Dict[str, Any], parallel_workers: int) -> Dict[str, Any]:
    validation = _validate_batch_inputs(module, effective_inputs)
    if not validation.get("batch_mode"):
        raise HTTPException(status_code=400, detail="该模块没有配置批处理输入字段 batch_role")
    if validation["missing"]:
        raise HTTPException(status_code=400, detail=_format_batch_validation_error(validation))

    matched_jobs = validation.get("matched_jobs") or []
    if not matched_jobs:
        raise HTTPException(status_code=400, detail="没有匹配到可运行的批处理 job，请检查输入目录和文件名")

    jobs: List[Dict[str, Any]] = []
    for item in matched_jobs:
        job_inputs = _build_single_job_inputs_from_batch(module, effective_inputs, item["role_files"], item["slot"], item.get("output_dir") or "")
        _validate_required_inputs(module, job_inputs, skip_control_only=True)
        command, working_dir, runtime_env = build_runtime_for_module(module, job_inputs)
        jobs.append({"command": command, "inputs": job_inputs, "working_dir": working_dir, "env": runtime_env})

    parent_task = task_manager.submit_batch_group(
        module_id=module["id"],
        module_name=module.get("name", module["id"]),
        jobs=jobs,
        max_parallel=clamp_parallel_workers(parallel_workers),
    )
    return {"ok": True, "batch_mode": True, "parent_task": parent_task, "parent_task_id": parent_task.get("id") if isinstance(parent_task, dict) else None, "matched_count": len(jobs), "parallel_workers": clamp_parallel_workers(parallel_workers), "extras": validation["extras"]}

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

import shutil
import uuid
from pathlib import Path


def is_output_field(field: dict) -> bool:
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


def is_input_path_field(field: dict) -> bool:
    if is_output_field(field):
        return False

    field_type = str(field.get("type", "")).lower()
    key = str(field.get("key", "")).lower()
    label = str(field.get("label", "")).lower()

    if field_type not in {"file_path", "dir_path"}:
        return False

    input_keywords = [
        "input",
        "file",
        "path",
        "dir",
        "folder",
        "输入",
        "文件",
        "目录",
    ]

    return any(k in key or k in label for k in input_keywords)

def get_field_dir_name(field: dict) -> str:
    label = str(field.get("label") or "").strip()
    key = str(field.get("key") or "").strip()
    return safe_dir_name(label or key or "输入")


def copy_input_to_managed_field_dir(value: str, input_root_dir: Path, target_dir: Path) -> str:
    """
    输入文件/文件夹复制到：
    uploads/用户名/输入文件夹/字段名/
    """
    if not value:
        return value

    src = Path(str(value))
    if not src.exists():
        return value

    src_resolved = src.resolve()
    input_root_resolved = input_root_dir.resolve()

    try:
        src_resolved.relative_to(input_root_resolved)
        return str(src_resolved)
    except ValueError:
        pass

    target_dir.mkdir(parents=True, exist_ok=True)

    if src.is_file():
        target = target_dir / src.name
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            idx = 1
            while True:
                candidate = target_dir / f"{stem}_{idx}{suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                idx += 1

        shutil.copy2(src, target)
        return str(target.resolve())

    if src.is_dir():
        shutil.copytree(src, target_dir, dirs_exist_ok=True)
        return str(target_dir.resolve())

    return value


def copy_output_to_managed_dir(src_dir: Path, dst_dir: Path):
    """
    把本地输出目录内容复制到右侧文件管理的输出文件夹。
    支持递归复制文件和子文件夹。
    """
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)

    if not src_dir.exists():
        raise FileNotFoundError(f"源输出目录不存在: {src_dir}")

    if not src_dir.is_dir():
        raise NotADirectoryError(f"源输出路径不是文件夹: {src_dir}")

    dst_dir.mkdir(parents=True, exist_ok=True)

    for item in src_dir.rglob("*"):
        rel = item.relative_to(src_dir)
        target = dst_dir / rel

        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def start_output_sync_after_task(task_id: str, output_syncs: list[tuple[str, str]]):
    """
    任务结束后，把原始输出目录同步到 uploads/用户名/输出文件夹/xxx。
    注意：有些 exe 即使生成了结果，也可能返回非 0 导致任务状态为 failed；
    所以这里在 success / failed 时都尝试同步，cancelled 不同步。
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
                if status != "cancelled":
                    for src, dst in output_syncs:
                        try:
                            task_manager.append_log(
                                task_id,
                                f"[OUTPUT-SYNC] 开始同步输出目录: {src} -> {dst}",
                            )
                            copy_output_to_managed_dir(Path(src), Path(dst))
                            task_manager.append_log(
                                task_id,
                                f"[OUTPUT-SYNC] 输出目录同步完成: {src} -> {dst}",
                            )
                        except Exception as exc:
                            task_manager.append_log(
                                task_id,
                                f"[OUTPUT-SYNC-ERROR] 输出目录同步失败: {src} -> {dst}; {repr(exc)}",
                            )
                return

            time.sleep(2)

    threading.Thread(target=worker, daemon=True).start()

def safe_dir_name(name: str) -> str:
    name = str(name or "").strip()
    name = name.replace("..", "_")
    name = name.replace("/", "_").replace("\\", "_")
    name = name.replace(":", "_").replace("*", "_").replace("?", "_")
    name = name.replace('"', "_").replace("<", "_").replace(">", "_").replace("|", "_")
    return name or "未命名目录"

def normalize_task_io_paths(module: dict, inputs: dict, username: str) -> tuple[dict, Path, list[tuple[str, str]]]:
    """
    运行任务前整理路径：
    1. 输入文件/文件夹复制到 uploads/用户名/输入文件夹/字段名/
    2. 输出目录保留用户原始选择，让 exe 正常写结果
    3. 同时准备同步规则：任务成功后复制到 uploads/用户名/输出文件夹/输出目录名/
    """
    user_dir, input_dir, output_dir = get_user_upload_dirs(username)

    new_inputs = dict(inputs)
    output_syncs: list[tuple[str, str]] = []

    final_output_dir = output_dir / "输出结果"

    for field in module.get("inputs", []) or []:
        key = field.get("key")
        if not key:
            continue

        field_type = str(field.get("type", "")).lower()

        if is_output_field(field):
            original_value = str(new_inputs.get(key) or "").strip()

            if original_value:
                source_output_dir = Path(original_value).resolve()
                output_name = safe_dir_name(source_output_dir.name)
            else:
                output_name = safe_dir_name(field.get("label") or key or "输出结果")
                source_output_dir = output_dir / output_name
                new_inputs[key] = str(source_output_dir.resolve())

            final_output_dir = output_dir / output_name
            final_output_dir.mkdir(parents=True, exist_ok=True)

            if field_type == "dir_path":
                # 关键：这里不再改成 job_xxx，也不强行覆盖用户选择的 NC_1。
                # exe 仍然写入用户选择的原始输出目录。
                if original_value:
                    new_inputs[key] = str(source_output_dir)
                else:
                    new_inputs[key] = str(final_output_dir.resolve())

                if source_output_dir.resolve() != final_output_dir.resolve():
                    output_syncs.append((str(source_output_dir), str(final_output_dir.resolve())))

            else:
                suffix = str(field.get("output_ext") or ".tif")
                if not suffix.startswith("."):
                    suffix = "." + suffix

                if original_value:
                    new_inputs[key] = str(source_output_dir)
                    output_syncs.append((str(source_output_dir.parent), str(final_output_dir.resolve())))
                else:
                    new_inputs[key] = str((final_output_dir / f"{key}{suffix}").resolve())

        elif is_input_path_field(field):
            # 管理员固定输入/隐藏输入通常是模块 resources 下的文件，不能复制成用户上传输入，
            # 否则批处理 job 里容易出现资源路径丢失或旧路径被覆盖的问题。
            visible = field.get("visible_to_user", True) is not False
            admin_fixed = bool(field.get("admin_fixed", False)) or not visible
            if admin_fixed or field.get("path_mode") == "relative_to_module":
                value = new_inputs.get(key)
                if value not in ("", None):
                    new_inputs[key] = resolve_input_value_for_module(module, field, value)
                continue

            value = new_inputs.get(key)
            if value:
                field_dir = input_dir / get_field_dir_name(field)
                new_inputs[key] = copy_input_to_managed_field_dir(
                    value=value,
                    input_root_dir=input_dir,
                    target_dir=field_dir,
                )

    new_inputs["_user_upload_dir"] = str(user_dir.resolve())
    new_inputs["_user_input_dir"] = str(input_dir.resolve())
    new_inputs["_user_output_dir"] = str(final_output_dir.resolve())

    return new_inputs, final_output_dir, output_syncs

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