from __future__ import annotations
import json
import time
import os
import shutil
import time
import subprocess
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from .tif_tiles import merge_tif_tiles

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

TERMINAL_STATUSES = {"success", "failed", "cancelled"}

class TaskManager:
    def __init__(self, tasks_file: str | Path):
        self.tasks_file = Path(tasks_file)
        self.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        self.base_dir = self.tasks_file.parent.parent
        self.runtime_dir = Path(os.environ.get("LOCAL_WEB_RUNTIME_DIR") or (self.base_dir / "runtime")).resolve()
        self.parallel_chunks_dir = self.runtime_dir / "parallel_chunks"
        self.parallel_chunks_dir.mkdir(parents=True, exist_ok=True)

        self.lock = threading.RLock()
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.processes: Dict[str, subprocess.Popen] = {}
        self.cancel_flags: set[str] = set()

        # CPU 亲和性：把每个 exe 绑定到固定 CPU 核组，避免多个 exe 自由抢占全部 CPU。
        # 默认启用；如需关闭：set LOCAL_WEB_CPU_AFFINITY=0
        self.affinity_lock = threading.RLock()
        self.task_cpu_affinity: Dict[str, List[int]] = {}
        self.cpu_affinity_enabled = self._env_bool("LOCAL_WEB_CPU_AFFINITY", True)
        self.cpu_affinity_reserved_cores = max(0, int(os.environ.get("LOCAL_WEB_RESERVED_CORES", "2") or 2))
        self.cpu_affinity_cores_per_process = max(1, int(os.environ.get("LOCAL_WEB_CORES_PER_PROCESS", "2") or 2))
        self.cpu_affinity_max_groups = max(0, int(os.environ.get("LOCAL_WEB_AFFINITY_MAX_GROUPS", "0") or 0))
        self.cpu_affinity_wait_seconds = max(0.05, float(os.environ.get("LOCAL_WEB_AFFINITY_WAIT_SECONDS", "0.2") or 0.2))
        self.cpu_affinity_set_thread_env = self._env_bool("LOCAL_WEB_AFFINITY_SET_THREAD_ENV", True)
        self.cpu_affinity_include_children = self._env_bool("LOCAL_WEB_AFFINITY_INCLUDE_CHILDREN", True)
        self.cpu_affinity_core_groups: List[List[int]] = []

        # 运行调度器：根据本机 CPU 核数控制同时运行的模块进程数。
        # 遥感反演通常会加载大模型/大数组，CPU 核数不能直接等于安全并发。
        # 这版采用保守默认值：
        # - 建议值最高 2；16 核/24 核机器也默认建议 2。
        # - 上限值最高 4；用户可以选更高，但后端仍会按 CPU/内存/磁盘压力自动降级或排队。
        # 如需手动覆盖，可设置环境变量：
        # LOCAL_WEB_SUGGESTED_PROCESS_SLOTS / LOCAL_WEB_MAX_PROCESS_SLOTS。
        self.cpu_count = max(1, int(os.cpu_count() or 1))
        # 放宽默认值：建议数更接近实际可用进程池。
        # 16 核/24 核默认建议 4，上限 8；用户选择后不再因为固定模型大小直接砍成 1。
        default_suggested_slots = max(1, min(4, (self.cpu_count + 3) // 4))
        default_max_slots = max(default_suggested_slots, min(8, max(4, (self.cpu_count + 2) // 3)))

        try:
            env_suggested_slots = int(os.environ.get("LOCAL_WEB_SUGGESTED_PROCESS_SLOTS", "") or default_suggested_slots)
        except Exception:
            env_suggested_slots = default_suggested_slots

        try:
            env_max_slots = int(os.environ.get("LOCAL_WEB_MAX_PROCESS_SLOTS", "") or default_max_slots)
        except Exception:
            env_max_slots = default_max_slots

        self.max_process_slots = max(1, min(self.cpu_count, env_max_slots))

        # 根据 CPU 亲和性配置生成核组。默认保留前 2 个逻辑核给系统/浏览器/后端，
        # 每个 exe 分配 2 个逻辑核。核组数量会反向限制最大并行 exe 数，避免选择了
        # 8 个进程但只剩 3 组 CPU 核可用。
        self.cpu_affinity_core_groups = self._build_cpu_affinity_groups()
        if self.cpu_affinity_enabled and self.cpu_affinity_core_groups:
            self.max_process_slots = min(self.max_process_slots, len(self.cpu_affinity_core_groups))

        self.suggested_process_slots = max(1, min(self.max_process_slots, env_suggested_slots))
        # 顶层排队只做“临界保护”。一般负载不阻止父任务启动，避免一直 queued。
        self.cpu_busy_threshold = float(os.environ.get("LOCAL_WEB_CPU_QUEUE_THRESHOLD", "99"))
        self.scheduler_queue: list[Dict[str, Any]] = []
        self.active_slots: Dict[str, int] = {}
        self.drain_lock = threading.Lock()
        # 运行中保护：批处理/并行任务启动子进程前会检查 CPU、内存和磁盘压力，压力过高时暂停启动新子任务。
        # 子进程启动保护：逐个启动子任务；达到阈值时暂停启动新的子任务。
        self.child_launch_cpu_threshold = float(os.environ.get("LOCAL_WEB_CHILD_START_CPU_THRESHOLD", "99"))
        self.child_launch_memory_threshold = float(os.environ.get("LOCAL_WEB_CHILD_START_MEMORY_THRESHOLD", "99"))
        self.child_launch_min_memory_gb = float(os.environ.get("LOCAL_WEB_CHILD_START_MIN_MEMORY_GB", "0.3"))
        self.child_launch_disk_threshold = float(os.environ.get("LOCAL_WEB_CHILD_START_DISK_THRESHOLD", "99.5"))
        self.child_launch_min_disk_free_gb = float(os.environ.get("LOCAL_WEB_CHILD_START_MIN_DISK_FREE_GB", "0.5"))
        self.child_launch_wait_seconds = float(os.environ.get("LOCAL_WEB_CHILD_START_WAIT_SECONDS", "2"))
        self.child_start_stagger_seconds = float(os.environ.get("LOCAL_WEB_CHILD_START_STAGGER_SECONDS", "0.5"))
        self.fast_refill_cpu_threshold = float(os.environ.get("LOCAL_WEB_FAST_REFILL_CPU_THRESHOLD", "99.5"))
        self.fast_refill_memory_threshold = float(os.environ.get("LOCAL_WEB_FAST_REFILL_MEMORY_THRESHOLD", "97"))
        self.fast_refill_min_memory_gb = float(os.environ.get("LOCAL_WEB_FAST_REFILL_MIN_MEMORY_GB", "1.0"))
        self.fast_refill_wait_seconds = float(os.environ.get("LOCAL_WEB_FAST_REFILL_WAIT_SECONDS", "0.4"))
        self.adaptive_child_start_enabled = str(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START", "1")).strip().lower() not in {"0", "false", "no", "off"}
        self.adaptive_child_start_min_interval = float(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START_MIN_SECONDS", "5"))
        self.adaptive_child_start_max_interval = float(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START_MAX_SECONDS", "60"))
        self.adaptive_child_start_sample_seconds = float(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START_SAMPLE_SECONDS", "1.5"))
        self.adaptive_child_start_decline_threshold = float(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START_CPU_DECLINE", "10"))
        self.adaptive_child_start_stable_samples = max(1, int(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START_STABLE_SAMPLES", "3")))
        self.adaptive_child_start_max_probe_seconds = float(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START_MAX_PROBE_SECONDS", "90"))
        self.adaptive_child_start_min_peak_cpu = float(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START_MIN_PEAK_CPU", "60"))
        self.adaptive_child_start_memory_threshold = float(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START_MEMORY_THRESHOLD", "90"))
        self.adaptive_child_start_min_memory_gb = float(os.environ.get("LOCAL_WEB_ADAPTIVE_CHILD_START_MIN_MEMORY_GB", "1.0"))
        self.learned_child_start_intervals: Dict[str, float] = {}
        self.child_start_gate_locks: Dict[str, threading.Lock] = {}
        self._last_pressure_log_at: Dict[str, float] = {}
        self.parallel_progress_scan_interval_seconds = float(os.environ.get("LOCAL_WEB_PARALLEL_PROGRESS_SCAN_SECONDS", "1.0"))
        self.parallel_monitor_interval_seconds = float(os.environ.get("LOCAL_WEB_PARALLEL_MONITOR_LOG_SECONDS", "20"))
        self.dynamic_worker_boost_enabled = str(os.environ.get("LOCAL_WEB_DYNAMIC_WORKER_BOOST", "1")).strip().lower() not in {"0", "false", "no", "off"}
        # 启用 CPU 亲和性时，动态额外 worker 会破坏“一个 exe 一个固定核组”的稳定性，默认关闭。
        if self.cpu_affinity_enabled:
            self.dynamic_worker_boost_enabled = False
        self.dynamic_worker_boost_extra = max(0, int(os.environ.get("LOCAL_WEB_DYNAMIC_WORKER_BOOST_EXTRA", "1") or 1))
        self.dynamic_worker_boost_cpu_below = float(os.environ.get("LOCAL_WEB_DYNAMIC_WORKER_BOOST_CPU_BELOW", "55"))
        self.dynamic_worker_boost_memory_below = float(os.environ.get("LOCAL_WEB_DYNAMIC_WORKER_BOOST_MEMORY_BELOW", "88"))
        self.dynamic_worker_boost_min_memory_gb = float(os.environ.get("LOCAL_WEB_DYNAMIC_WORKER_BOOST_MIN_MEMORY_GB", "2.0"))
        self._last_parallel_monitor_log_at: Dict[str, float] = {}
        self._last_dynamic_boost_log_at: Dict[str, float] = {}

        # 性能优化：日志高频输出时，不再每一行都把完整 tasks.json 写回磁盘。
        # 前端读取任务时仍然直接读内存中的 logs；这里只是把持久化写盘做成短时间合并。
        self.task_save_debounce_seconds = float(os.environ.get("LOCAL_WEB_TASK_SAVE_DEBOUNCE_SECONDS", "0.8"))
        self.max_logs_per_task = max(200, int(os.environ.get("LOCAL_WEB_MAX_LOG_LINES_PER_TASK", "2000")))
        self._save_dirty = False
        self._save_timer: threading.Timer | None = None

        self._load_tasks()
        self._mark_interrupted_tasks()
        self._scheduler_heartbeat_thread = threading.Thread(
            target=self._scheduler_heartbeat,
            daemon=True,
        )
        self._scheduler_heartbeat_thread.start()
    def kick_scheduler(self):
        """
        外部主动唤醒调度器。
        前端轮询 /api/tasks 或 /api/system/resources 时调用，
        防止任务已经 queued 但调度器没有继续 drain。
        """
        try:
            self._drain_scheduler_queue()
        except Exception as exc:
            try:
                with self.lock:
                    for item in self.scheduler_queue:
                        task_id = str(item.get("task_id") or "")
                        task = self.tasks.get(task_id)
                        if task and task.get("status") == "queued":
                            task["queue_reason"] = (
                                f"调度器唤醒失败: {type(exc).__name__}: {exc}"
                            )
                self._save_tasks()
            except Exception:
                pass
    def kick_scheduler(self):
        """
        外部主动唤醒调度器。
        前端轮询 /api/tasks、/api/tasks/{id}、/api/system/resources 时调用。
        """
        try:
            self._drain_scheduler_queue()
        except Exception as exc:
            try:
                with self.lock:
                    for item in self.scheduler_queue:
                        task_id = str(item.get("task_id") or "")
                        task = self.tasks.get(task_id)
                        if task and task.get("status") == "queued":
                            task["queue_reason"] = (
                                f"调度器唤醒失败: {type(exc).__name__}: {exc}"
                            )
                self._save_tasks()
            except Exception:
                pass
    def _normalize_cleanup_roots(self, roots: Any) -> list[Path]:
        """规范化需要清理的 runtime 子目录。

        安全限制：只允许删除 backend/runtime/parallel_chunks 内部目录，
        防止误删用户输入数据、输出目录或模块安装目录。
        """
        if not roots:
            return []

        if isinstance(roots, (str, Path)):
            items = [roots]
        elif isinstance(roots, (list, tuple, set)):
            items = list(roots)
        else:
            return []

        try:
            allowed_root = self.parallel_chunks_dir.resolve()
        except Exception:
            return []

        result: list[Path] = []
        seen: set[str] = set()

        for item in items:
            try:
                p = Path(str(item)).resolve()
            except Exception:
                continue

            try:
                p.relative_to(allowed_root)
            except ValueError:
                continue

            key = str(p).lower()
            if key not in seen:
                seen.add(key)
                result.append(p)

        return result

    def _cleanup_runtime_roots(self, task_id: str, roots: Any, reason: str = "任务结束"):
        """清理平台拆分产生的 runtime/parallel_chunks 临时输入目录。"""
        paths = self._normalize_cleanup_roots(roots)
        if not paths:
            return

        removed = 0
        for path in paths:
            try:
                if path.exists() and path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except Exception as exc:
                try:
                    self.append_log(task_id, f"[CLEANUP] 清理临时输入目录失败: {path}，原因: {type(exc).__name__}: {exc}")
                except Exception:
                    pass

        if removed:
            try:
                self.append_log(task_id, f"[CLEANUP] {reason}，已清理 {removed} 个平台拆分临时输入目录，避免占用磁盘空间。")
            except Exception:
                pass

    def _cleanup_runtime_roots_for_task(self, task_id: str, reason: str = "任务结束"):
        task = self.get_task(task_id) or {}
        roots = task.get("cleanup_roots") or (task.get("inputs") or {}).get("_parallel_cleanup_roots")
        self._cleanup_runtime_roots(task_id, roots, reason=reason)

    def _scheduler_heartbeat(self):
        """
        调度器心跳。
        防止某次 enqueue/drain 因异常或热重载时机导致 queued 任务没有被启动。
        """
        while True:
            time.sleep(1.0)
            try:
                with self.lock:
                    has_queued = any(
                        (self.tasks.get(str(item.get("task_id") or "")) or {}).get("status") == "queued"
                        for item in self.scheduler_queue
                    )

                if has_queued:
                    self._drain_scheduler_queue()
            except Exception:
                pass
    def _load_tasks(self):
        if not self.tasks_file.exists():
            self.tasks = {}
            return

        try:
            raw = json.loads(self.tasks_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self.tasks = {
                    item["id"]: item
                    for item in raw
                    if isinstance(item, dict) and item.get("id")
                }
            elif isinstance(raw, dict):
                self.tasks = raw
            else:
                self.tasks = {}
        except Exception:
            self.tasks = {}

    def _mark_interrupted_tasks(self):
        """服务重启后，内存里的进程和调度队列都不存在了。把旧的 running/queued 标记为 cancelled，并清理平台拆分临时目录。"""
        changed = False
        cleanup_map: dict[str, Any] = {}
        with self.lock:
            for task in self.tasks.values():
                if task.get("status") in {"queued", "running"}:
                    task["status"] = "cancelled"
                    task["ended_at"] = now_iso()
                    task.setdefault("logs", []).append("[SYSTEM] 服务已重启，历史未完成任务已自动取消")
                    roots = task.get("cleanup_roots") or (task.get("inputs") or {}).get("_parallel_cleanup_roots")
                    if roots:
                        cleanup_map[str(task.get("id") or "")] = roots
                    changed = True
        if changed:
            self._save_tasks()
            for task_id, roots in cleanup_map.items():
                self._cleanup_runtime_roots(task_id, roots, reason="服务重启后清理历史临时输入目录")

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        value = os.environ.get(name)
        if value is None or str(value).strip() == "":
            return bool(default)
        return str(value).strip().lower() not in {"0", "false", "no", "off", "disable", "disabled"}

    @staticmethod
    def _format_core_range(cores: List[int] | None) -> str:
        cores = sorted(int(x) for x in (cores or []))
        if not cores:
            return ""
        ranges: list[str] = []
        start = prev = cores[0]
        for core in cores[1:]:
            if core == prev + 1:
                prev = core
                continue
            ranges.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = core
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        return ",".join(ranges)

    def _build_cpu_affinity_groups(self) -> List[List[int]]:
        """生成 CPU 核组，例如 24 核、保留 2 核、每进程 2 核 => [2,3], [4,5] ...。"""
        if not self.cpu_affinity_enabled:
            return []

        cpu_count = max(1, int(self.cpu_count or os.cpu_count() or 1))
        cores_per_process = max(1, min(cpu_count, int(self.cpu_affinity_cores_per_process or 1)))

        # CPU 很少时不强行保留 2 个核，避免无核可分配。
        reserve = max(0, int(self.cpu_affinity_reserved_cores or 0))
        if cpu_count <= 2:
            reserve = 0
        elif reserve >= cpu_count:
            reserve = max(0, cpu_count - cores_per_process)
        reserve = max(0, min(reserve, max(0, cpu_count - 1)))

        usable = list(range(reserve, cpu_count))
        groups: List[List[int]] = []
        for i in range(0, len(usable), cores_per_process):
            group = usable[i:i + cores_per_process]
            if len(group) == cores_per_process:
                groups.append(group)

        if not groups and usable:
            groups = [usable]

        if self.cpu_affinity_max_groups > 0:
            groups = groups[:self.cpu_affinity_max_groups]

        return groups

    def _cpu_affinity_policy_snapshot(self) -> Dict[str, Any]:
        with self.affinity_lock:
            active = {
                task_id: {
                    "cores": list(cores),
                    "label": self._format_core_range(cores),
                }
                for task_id, cores in self.task_cpu_affinity.items()
            }
            used = len(active)

        return {
            "enabled": bool(self.cpu_affinity_enabled),
            "reserved_cores": int(self.cpu_affinity_reserved_cores),
            "cores_per_process": int(self.cpu_affinity_cores_per_process),
            "set_thread_env": bool(self.cpu_affinity_set_thread_env),
            "include_children": bool(self.cpu_affinity_include_children),
            "groups": [list(g) for g in self.cpu_affinity_core_groups],
            "group_labels": [self._format_core_range(g) for g in self.cpu_affinity_core_groups],
            "total_groups": len(self.cpu_affinity_core_groups),
            "used_groups": used,
            "free_groups": max(0, len(self.cpu_affinity_core_groups) - used),
            "active_assignments": active,
        }

    def _acquire_cpu_affinity_group(self, task_id: str) -> List[int]:
        """为一个即将启动的 exe 申请 CPU 核组。无可用组时短暂等待。"""
        if not self.cpu_affinity_enabled or not self.cpu_affinity_core_groups:
            return []

        while task_id not in self.cancel_flags:
            with self.affinity_lock:
                existing = self.task_cpu_affinity.get(task_id)
                if existing:
                    return list(existing)

                used = {tuple(v) for v in self.task_cpu_affinity.values()}
                for group in self.cpu_affinity_core_groups:
                    key = tuple(group)
                    if key not in used:
                        self.task_cpu_affinity[task_id] = list(group)
                        return list(group)

            time.sleep(self.cpu_affinity_wait_seconds)

        return []

    def _release_cpu_affinity_group(self, task_id: str) -> None:
        if not self.cpu_affinity_enabled:
            return
        with self.affinity_lock:
            self.task_cpu_affinity.pop(task_id, None)

    def _inject_affinity_thread_env(self, env: Dict[str, str], cores: List[int]) -> Dict[str, str]:
        """把单 exe 内部线程数限制到核组大小，减少 OpenMP/MKL/GDAL 等库的线程争抢。"""
        if not cores or not self.cpu_affinity_set_thread_env:
            return env

        thread_count = max(1, len(cores))
        for key in (
            "OPENBLAS_NUM_THREADS",
            "OMP_NUM_THREADS",
            "OMP_THREAD_LIMIT",
            "GOTO_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "LOKY_MAX_CPU_COUNT",
            "OPENCV_FOR_THREADS_NUM",
            "GDAL_NUM_THREADS",
            "LOCAL_WEB_RUNTIME_THREADS",
        ):
            env[key] = str(thread_count)
        env["LOCAL_WEB_CPU_AFFINITY_CORES"] = ",".join(str(x) for x in cores)
        env["LOCAL_WEB_CPU_AFFINITY_LABEL"] = self._format_core_range(cores)
        return env

    def _apply_cpu_affinity_to_process(self, pid: int, cores: List[int]) -> tuple[bool, str]:
        """把进程绑定到指定 CPU 核组；Windows/Linux/macOS 能力取决于 psutil/系统支持。"""
        if not self.cpu_affinity_enabled or not cores:
            return False, "CPU 亲和性未启用或未分配核组"

        # 首选 psutil：Windows / Linux 都支持 cpu_affinity。
        try:
            import psutil  # type: ignore
            proc = psutil.Process(pid)
            proc.cpu_affinity(list(cores))
            if self.cpu_affinity_include_children:
                for child in proc.children(recursive=True):
                    try:
                        child.cpu_affinity(list(cores))
                    except Exception:
                        continue
            return True, ""
        except Exception as exc:
            psutil_error = repr(exc)

        # Linux 兜底。
        try:
            if hasattr(os, "sched_setaffinity"):
                os.sched_setaffinity(pid, set(cores))  # type: ignore[attr-defined]
                return True, ""
        except Exception as exc:
            return False, f"设置 CPU 亲和性失败：psutil={psutil_error}；sched_setaffinity={repr(exc)}"

        return False, f"设置 CPU 亲和性失败：{psutil_error}。请安装 psutil，或关闭 LOCAL_WEB_CPU_AFFINITY。"

    def _system_cpu_percent(self) -> float | None:
        """读取本机 CPU 使用率。

        优先使用 psutil；没有 psutil 时，在 Windows 下用 wmic/typeperf 兜底，
        避免前端一直显示 “-”。
        """
        try:
            import psutil  # type: ignore
            return float(psutil.cpu_percent(interval=0.35))
        except Exception:
            pass

        if os.name == "nt":
            # 方式 1：wmic，很多 Windows 仍可用。
            try:
                result = subprocess.run(
                    ["wmic", "cpu", "get", "loadpercentage", "/value"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=3,
                    shell=False,
                )
                text = (result.stdout or "") + "\n" + (result.stderr or "")
                values = []
                for line in text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("loadpercentage="):
                        values.append(float(line.split("=", 1)[1].strip()))
                if values:
                    return max(0.0, min(100.0, sum(values) / len(values)))
            except Exception:
                pass

            # 方式 2：typeperf。
            try:
                result = subprocess.run(
                    ["typeperf", r"\\Processor(_Total)\\% Processor Time", "-sc", "1"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=5,
                    shell=False,
                )
                import re
                nums = re.findall(r'"[^"]+","([0-9.]+)"', result.stdout or "")
                if nums:
                    return max(0.0, min(100.0, float(nums[-1])))
            except Exception:
                pass

        try:
            if hasattr(os, "getloadavg"):
                load1, _, _ = os.getloadavg()
                return max(0.0, min(100.0, float(load1) / max(1, self.cpu_count) * 100.0))
        except Exception:
            pass
        return None

    def _active_process_count(self) -> int:
        """当前平台真实启动、尚未退出的子进程数。"""
        count = 0
        with self.lock:
            processes = list(self.processes.values())
        for process in processes:
            try:
                if process and process.poll() is None:
                    count += 1
            except Exception:
                continue
        return count

    def _running_process_cpu_percent(self) -> float | None:
        """读取平台启动的模块进程 CPU 占用总和。

        psutil 的 cpu_percent(interval=0.0) 第一次常返回 0，所以这里用短采样；
        同时把子进程的子进程也统计进去，避免前端一直显示 0。
        """
        try:
            import psutil  # type: ignore
        except Exception:
            return None

        total = 0.0
        with self.lock:
            processes = list(self.processes.values())
        for process in processes:
            try:
                if process.poll() is None:
                    proc = psutil.Process(process.pid)
                    total += float(proc.cpu_percent(interval=0.03))
                    for child in proc.children(recursive=True):
                        try:
                            if child.is_running():
                                total += float(child.cpu_percent(interval=0.0))
                        except Exception:
                            continue
            except Exception:
                continue
        return round(total, 2)

    def _used_slots_locked(self) -> int:
        return sum(max(1, int(v or 1)) for v in self.active_slots.values())

    def _normalize_requested_slots(self, value: int | str | None) -> int:
        try:
            n = int(value or 1)
        except Exception:
            n = 1
        return max(1, min(n, self.max_process_slots))

    def _remove_from_scheduler_queue_locked(self, task_id: str):
        self.scheduler_queue = [item for item in self.scheduler_queue if item.get("task_id") != task_id]
        self._refresh_queue_positions_locked()

    def _refresh_queue_positions_locked(self):
        pos = 1
        used = self._used_slots_locked()
        for item in self.scheduler_queue:
            tid = str(item.get("task_id") or "")
            task = self.tasks.get(tid)
            if not task or task.get("status") != "queued":
                continue
            requested = self._normalize_requested_slots(item.get("requested_slots"))
            task["queue_position"] = pos
            if used == 0 and requested <= self.max_process_slots:
                task["queue_reason"] = (
                    f"调度器等待启动：当前占用 {used}/{self.max_process_slots}，"
                    f"本任务需要 {requested} 个进程槽。若长时间不启动，请检查后端是否使用 --reload 或调度器是否异常。"
                )
            else:
                task["queue_reason"] = (
                    f"等待本地 CPU 空闲：当前占用 {used}/{self.max_process_slots}，"
                    f"本任务需要 {requested} 个进程槽"
                )
            pos += 1


    def _virtual_memory_snapshot(self) -> Dict[str, Any]:
        try:
            import psutil  # type: ignore
            mem = psutil.virtual_memory()
            return {
                "percent": float(mem.percent),
                "available_gb": float(mem.available or 0) / (1024 ** 3),
            }
        except Exception:
            pass

        if os.name == "nt":
            # wmic: FreePhysicalMemory/TotalVisibleMemorySize 单位是 KB。
            try:
                result = subprocess.run(
                    ["wmic", "OS", "get", "FreePhysicalMemory,TotalVisibleMemorySize", "/value"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=3,
                    shell=False,
                )
                vals = {}
                for line in (result.stdout or "").splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        vals[k.strip().lower()] = float(v.strip() or 0)
                free_kb = vals.get("freephysicalmemory")
                total_kb = vals.get("totalvisiblememorysize")
                if free_kb and total_kb:
                    used_percent = max(0.0, min(100.0, (1.0 - free_kb / total_kb) * 100.0))
                    return {
                        "percent": used_percent,
                        "available_gb": free_kb / 1024.0 / 1024.0,
                    }
            except Exception:
                pass
        return {"percent": None, "available_gb": None}

    def _disk_usage_snapshot(self) -> Dict[str, Any]:
        try:
            path = self.tasks_file.parent.resolve()
        except Exception:
            path = Path(".")
        try:
            import psutil  # type: ignore
            usage = psutil.disk_usage(str(path))
            return {
                "percent": float(usage.percent),
                "free_gb": float(usage.free or 0) / (1024 ** 3),
            }
        except Exception:
            try:
                usage = shutil.disk_usage(str(path))
                total = float(usage.total or 1)
                return {
                    "percent": float(usage.used) / total * 100.0,
                    "free_gb": float(usage.free or 0) / (1024 ** 3),
                }
            except Exception:
                return {"percent": None, "free_gb": None}

    def _runtime_pressure_reason(self, fast_refill: bool = False) -> str:
        """返回是否应该暂停启动新的子进程。

        轻量化策略：
        - 顶层任务不因为内存 80% 多就长期 queued；
        - 真正启动每一个子进程前，才检查 CPU/内存/磁盘和当前平台进程数；
        - 已启动的进程不强杀，只暂停后续启动，等负载下降再继续。
        """
        active_processes = self._active_process_count()
        if active_processes >= self.max_process_slots:
            return f"平台已启动 {active_processes}/{self.max_process_slots} 个模块进程，等待已有进程完成"

        if self.cpu_affinity_enabled and self.cpu_affinity_core_groups:
            with self.affinity_lock:
                free_groups = len(self.cpu_affinity_core_groups) - len(self.task_cpu_affinity)
            if free_groups <= 0:
                return f"CPU 核组已全部占用：{len(self.task_cpu_affinity)}/{len(self.cpu_affinity_core_groups)}，等待已有 exe 完成后释放核组"

        if fast_refill or not self.adaptive_child_start_enabled:
            cpu = self._system_cpu_percent()
            cpu_threshold = self.fast_refill_cpu_threshold if fast_refill else self.child_launch_cpu_threshold
            if cpu is not None and cpu >= cpu_threshold:
                return f"CPU 使用率 {cpu:.1f}% 已超过暂停启动阈值 {cpu_threshold:.0f}%"

        mem = self._virtual_memory_snapshot()
        mem_percent = mem.get("percent")
        mem_available = mem.get("available_gb")
        memory_threshold = (
            max(self.child_launch_memory_threshold, self.fast_refill_memory_threshold)
            if fast_refill else self.child_launch_memory_threshold
        )
        min_memory_gb = (
            min(self.child_launch_min_memory_gb, self.fast_refill_min_memory_gb)
            if fast_refill else self.child_launch_min_memory_gb
        )
        if mem_percent is not None and mem_percent >= memory_threshold:
            return f"内存使用率 {mem_percent:.1f}% 已超过暂停启动阈值 {memory_threshold:.0f}%"
        if mem_available is not None and mem_available <= min_memory_gb:
            return f"可用内存仅 {mem_available:.1f}GB，低于最低阈值 {min_memory_gb:.1f}GB"

        disk = self._disk_usage_snapshot()
        disk_percent = disk.get("percent")
        disk_free = disk.get("free_gb")
        if disk_percent is not None and disk_percent >= self.child_launch_disk_threshold:
            return f"磁盘使用率 {disk_percent:.1f}% 已超过暂停启动阈值 {self.child_launch_disk_threshold:.0f}%"
        if disk_free is not None and disk_free <= self.child_launch_min_disk_free_gb:
            return f"磁盘剩余空间仅 {disk_free:.1f}GB，低于最低阈值 {self.child_launch_min_disk_free_gb:.1f}GB"

        return ""

    def _wait_until_safe_to_start_child(self, parent_id: str, label: str, fast_refill: bool = False):
        """父并行任务运行期间，系统压力过高时不再启动新子进程，等压力下降再继续。

        需要连续两次采样都安全才放行，避免刚启动几个进程时 CPU 还没来得及升高，
        后续进程又被瞬间全部拉起导致电脑卡死。
        """
        safe_samples = 0
        required_safe_samples = 1 if fast_refill else 2
        wait_seconds = self.fast_refill_wait_seconds if fast_refill else self.child_launch_wait_seconds
        while parent_id not in self.cancel_flags:
            reason = self._runtime_pressure_reason(fast_refill=fast_refill)
            if not reason:
                safe_samples += 1
                if safe_samples >= required_safe_samples:
                    return
                time.sleep(max(0.1, min(wait_seconds, 3.0)))
                continue

            safe_samples = 0
            now = time.time()
            last = self._last_pressure_log_at.get(parent_id, 0.0)
            if now - last >= 12:
                self._last_pressure_log_at[parent_id] = now
                self.append_log(
                    parent_id,
                    f"[SAFE] 暂停启动新子任务 {label}：{reason}。已启动的任务继续运行，等负载下降后自动继续，防止电脑卡死。",
                )
            time.sleep(max(0.1, wait_seconds))

    def _adaptive_start_memory_safe(self) -> tuple[bool, str]:
        mem = self._virtual_memory_snapshot()
        mem_percent = mem.get("percent")
        mem_available = mem.get("available_gb")
        if mem_percent is not None and mem_percent >= self.adaptive_child_start_memory_threshold:
            return False, f"内存使用率 {mem_percent:.1f}% 已超过自适应启动阈值 {self.adaptive_child_start_memory_threshold:.0f}%"
        if mem_available is not None and mem_available <= self.adaptive_child_start_min_memory_gb:
            return False, f"可用内存 {mem_available:.1f}GB 低于自适应启动阈值 {self.adaptive_child_start_min_memory_gb:.1f}GB"
        return True, ""

    def _learn_child_start_interval_after_first_launch(self, parent_id: str, label: str) -> float:
        min_interval = max(0.0, self.adaptive_child_start_min_interval)
        max_interval = max(min_interval, self.adaptive_child_start_max_interval)
        sample_seconds = max(0.2, self.adaptive_child_start_sample_seconds)
        decline_threshold = max(0.0, self.adaptive_child_start_decline_threshold)
        required_samples = max(1, self.adaptive_child_start_stable_samples)
        max_probe_seconds = max(min_interval, self.adaptive_child_start_max_probe_seconds)

        start_time = time.time()
        peak_cpu = 0.0
        decline_count = 0
        last_cpu: float | None = None
        self.append_log(
            parent_id,
            f"[ADAPTIVE] 已启动首个子任务 {label}，开始监测 CPU 峰值回落，用于学习后续子任务启动间隔。",
        )

        while parent_id not in self.cancel_flags:
            elapsed = time.time() - start_time
            cpu = self._system_cpu_percent()
            memory_safe, memory_reason = self._adaptive_start_memory_safe()

            if cpu is not None:
                peak_cpu = max(peak_cpu, cpu)
                has_peak = peak_cpu >= self.adaptive_child_start_min_peak_cpu
                dropped_from_peak = has_peak and cpu <= peak_cpu - decline_threshold
                moving_down = last_cpu is not None and cpu <= last_cpu
                if dropped_from_peak and moving_down:
                    decline_count += 1
                else:
                    decline_count = 0
                last_cpu = cpu

                if decline_count >= required_samples and memory_safe:
                    learned = max(min_interval, min(elapsed, max_interval))
                    self.append_log(
                        parent_id,
                        f"[ADAPTIVE] 学到子任务启动间隔 {learned:.1f}s：CPU峰值 {peak_cpu:.1f}%，当前 {cpu:.1f}%，连续回落 {decline_count} 次。",
                    )
                    return learned

                if not has_peak and elapsed >= min_interval and memory_safe:
                    self.append_log(
                        parent_id,
                        f"[ADAPTIVE] 首个子任务未形成明显 CPU 峰值，使用最小启动间隔 {min_interval:.1f}s。",
                    )
                    return min_interval

            if elapsed >= max_probe_seconds:
                learned = max(min_interval, min(elapsed, max_interval))
                reason = f"，内存暂不安全：{memory_reason}" if memory_reason else ""
                self.append_log(
                    parent_id,
                    f"[ADAPTIVE] CPU 回落探测达到上限，使用保守启动间隔 {learned:.1f}s{reason}。",
                )
                return learned

            time.sleep(sample_seconds)

        return max_interval

    def _sleep_before_adaptive_child_launch(self, parent_id: str, label: str, interval: float, last_launch_at: float) -> bool:
        if interval <= 0 or last_launch_at <= 0:
            return True
        remaining = interval - (time.time() - last_launch_at)
        if remaining <= 0:
            return True

        self.append_log(parent_id, f"[ADAPTIVE] 启动 {label} 前等待 {remaining:.1f}s，按首个子任务学习到的间隔错峰启动。")
        end_at = time.time() + remaining
        while parent_id not in self.cancel_flags:
            left = end_at - time.time()
            if left <= 0:
                return True
            time.sleep(min(0.5, max(0.05, left)))
        return False

    def get_system_resource_info(self) -> Dict[str, Any]:
        affinity_policy = self._cpu_affinity_policy_snapshot()
        with self.lock:
            running_workers = self._used_slots_locked()
            active_task_count = len(self.active_slots)
            queued_task_count = sum(
                1 for item in self.scheduler_queue
                if (self.tasks.get(str(item.get("task_id") or "")) or {}).get("status") == "queued"
            )
            active_tasks = []
            for task_id, slots in self.active_slots.items():
                task = self.tasks.get(task_id) or {}
                active_tasks.append({
                    "id": task_id,
                    "module_name": task.get("module_name") or "",
                    "requested_workers": slots,
                    "pid": task.get("pid"),
                    "status": task.get("status") or "running",
                    "cpu_affinity_cores": task.get("cpu_affinity_cores") or [],
                    "cpu_affinity_label": task.get("cpu_affinity_label") or "",
                    "runtime_threads": task.get("runtime_threads"),
                })

        cpu_percent = self._system_cpu_percent()
        process_cpu_percent = self._running_process_cpu_percent()
        mem_snapshot = self._virtual_memory_snapshot()
        disk_snapshot = self._disk_usage_snapshot()
        active_processes = self._active_process_count()
        return {
            "cpu_count": self.cpu_count,
            "suggested_workers": self.suggested_process_slots,
            "max_workers": self.max_process_slots,
            "adaptive_child_start": self.adaptive_child_start_enabled,
            "learned_child_start_intervals": dict(self.learned_child_start_intervals),
            "running_workers": active_processes,
            "available_workers": max(0, self.max_process_slots - active_processes),
            "active_task_count": active_task_count,
            "queued_task_count": queued_task_count,
            "cpu_percent": cpu_percent,
            "running_process_cpu_percent": process_cpu_percent,
            "cpu_busy_threshold": self.cpu_busy_threshold,
            "memory_percent": mem_snapshot.get("percent"),
            "memory_available_gb": mem_snapshot.get("available_gb"),
            "disk_percent": disk_snapshot.get("percent"),
            "disk_free_gb": disk_snapshot.get("free_gb"),
            "active_tasks": active_tasks,
            "cpu_affinity": affinity_policy,
            "cpu_affinity_enabled": affinity_policy.get("enabled"),
            "cpu_affinity_reserved_cores": affinity_policy.get("reserved_cores"),
            "cpu_affinity_cores_per_process": affinity_policy.get("cores_per_process"),
            "cpu_affinity_group_labels": affinity_policy.get("group_labels"),
            "cpu_affinity_total_groups": affinity_policy.get("total_groups"),
            "cpu_affinity_used_groups": affinity_policy.get("used_groups"),
            "cpu_affinity_free_groups": affinity_policy.get("free_groups"),
        }

    def _can_start_queued_item_locked(self, item: Dict[str, Any]) -> tuple[bool, str]:
        requested = self._normalize_requested_slots(item.get("requested_slots"))
        used = self._used_slots_locked()
        if used + requested > self.max_process_slots:
            return False, f"进程数超过本机安全上限：当前 {used}/{self.max_process_slots}，本任务需要 {requested}"

        # 顶层任务只按槽位排队，不再因为内存/磁盘中等压力长期 queued。
        # CPU/内存/磁盘保护放到子进程逐个启动前执行。
        return True, ""

    def _enqueue_task_runner(
        self,
        task_id: str,
        runner,
        args: tuple,
        requested_slots: int | str | None = 1,
    ):
        requested_slots = self._normalize_requested_slots(requested_slots)
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task["status"] = "queued"
            task["requested_workers"] = requested_slots
            task["queued_at"] = now_iso()
            task["queue_position"] = len(self.scheduler_queue) + 1
            task["queue_reason"] = "等待调度"
            self.scheduler_queue.append({
                "task_id": task_id,
                "runner": runner,
                "args": args,
                "requested_slots": requested_slots,
            })
            self._refresh_queue_positions_locked()
        self._save_tasks()
        self._drain_scheduler_queue()

    def _run_scheduled_item(self, item: Dict[str, Any]):
        task_id = str(item.get("task_id") or "")
        try:
            runner = item.get("runner")
            args = item.get("args") or ()
            if runner:
                runner(*args)
        finally:
            with self.lock:
                self.active_slots.pop(task_id, None)
            self._save_tasks()
            self._drain_scheduler_queue()

    def _drain_scheduler_queue(self):
        if not self.drain_lock.acquire(blocking=False):
            return
        try:
            while True:
                start_item: Dict[str, Any] | None = None
                with self.lock:
                    # 清理已取消/已删除的队列项。
                    while self.scheduler_queue:
                        candidate = self.scheduler_queue[0]
                        task_id = str(candidate.get("task_id") or "")
                        task = self.tasks.get(task_id)
                        if task and task.get("status") == "queued":
                            break
                        self.scheduler_queue.pop(0)

                    if not self.scheduler_queue:
                        self._refresh_queue_positions_locked()
                        return

                    item = self.scheduler_queue[0]
                    task_id = str(item.get("task_id") or "")
                    task = self.tasks.get(task_id)
                    can_start, reason = self._can_start_queued_item_locked(item)
                    if not can_start:
                        if task:
                            task["queue_reason"] = reason
                        self._refresh_queue_positions_locked()
                        self._save_tasks()
                        return

                    start_item = self.scheduler_queue.pop(0)
                    requested_slots = self._normalize_requested_slots(start_item.get("requested_slots"))
                    self.active_slots[task_id] = requested_slots
                    if task:
                        task["queue_position"] = None
                        task["queue_reason"] = ""
                        task["scheduled_at"] = now_iso()
                        task.setdefault("logs", []).append(
                            f"[SYSTEM] 已获得 {requested_slots} 个 CPU 进程槽，开始运行"
                        )
                    self._refresh_queue_positions_locked()
                    self._save_tasks()

                threading.Thread(
                    target=self._run_scheduled_item,
                    args=(start_item,),
                    daemon=True,
                ).start()
        finally:
            self.drain_lock.release()

    def _save_tasks(self):
        """立即把任务快照写入 tasks.json。

        只在状态变化、任务结束等关键位置直接调用。
        高频日志写入改由 _schedule_save_tasks() 合并，避免每输出一行日志就重写完整 JSON。
        """
        with self.lock:
            data = list(self.tasks.values())
            self.tasks_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _flush_scheduled_save(self):
        with self.lock:
            if not self._save_dirty:
                self._save_timer = None
                return
            self._save_dirty = False
            self._save_timer = None
        self._save_tasks()

    def _schedule_save_tasks(self):
        """把多次日志写盘合并到一次，降低 runtime 期间磁盘 I/O。"""
        if self.task_save_debounce_seconds <= 0:
            self._save_tasks()
            return

        with self.lock:
            self._save_dirty = True
            timer = self._save_timer
            if timer is not None and timer.is_alive():
                return
            self._save_timer = threading.Timer(
                self.task_save_debounce_seconds,
                self._flush_scheduled_save,
            )
            self._save_timer.daemon = True
            self._save_timer.start()

    def _trim_task_logs_locked(self, task: Dict[str, Any]):
        logs = task.get("logs")
        if not isinstance(logs, list):
            return
        if len(logs) <= self.max_logs_per_task:
            return

        keep_tail = max(100, self.max_logs_per_task - 1)
        removed = len(logs) - keep_tail
        task["logs"] = [
            f"[LOG-TRIM] 日志过长，已省略前 {removed} 行；可通过调大 LOCAL_WEB_MAX_LOG_LINES_PER_TASK 保留更多日志。"
        ] + logs[-keep_tail:]

    def list_tasks(self, owner_username: str | None = None) -> List[Dict[str, Any]]:
        with self.lock:
            items = list(self.tasks.values())

        if owner_username:
            owner_username = str(owner_username)
            items = [
                item for item in items
                if str(item.get("owner_username") or "") == owner_username
            ]

        items.sort(key=lambda x: x.get("started_at") or x.get("created_at") or "", reverse=True)
        return items

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return None
            return dict(task)

    def create_task(
        self,
        module_id: str,
        module_name: str,
        command: List[str],
        inputs: Dict[str, Any],
        kind: str = "module",
        extra: Dict[str, Any] | None = None,
        auto_save: bool = True,
    ) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        task = {
            "id": task_id,
            "module_id": module_id,
            "module_name": module_name,
            "kind": kind,
            "status": "queued",
            "return_code": None,
            "pid": None,
            "command": command,
            "inputs": inputs,
            "logs": [],
            "created_at": now_iso(),
            "started_at": None,
            "ended_at": None,
            "children": [],
            "owner_username": "",
        }
        if extra:
            task.update(extra)

        with self.lock:
            self.tasks[task_id] = task

        if auto_save:
            self._save_tasks()
        return task

    def append_log(self, task_id: str, text: str):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.setdefault("logs", []).append(str(text))
            self._trim_task_logs_locked(task)
        self._schedule_save_tasks()

    def _extract_error_lines_for_parent(self, child_task: Dict[str, Any], max_lines: int = 35) -> List[str]:
        """把子任务失败原因摘出来写回父任务日志。

        任务管理页现在只展示父任务，子任务被隐藏后，如果不把子任务 stderr/traceback
        汇总到父任务，用户只能看到“状态=failed”，无法定位算法报错。
        """
        logs = [str(x) for x in (child_task.get("logs") or [])]
        if not logs:
            return []

        important: List[str] = []
        capture_traceback = False
        for line in logs:
            text = str(line)
            low = text.lower()
            if (
                "[stderr]" in low
                or "traceback" in low
                or "error" in low
                or "exception" in low
                or "failed" in low
                or "错误" in text
                or "失败" in text
                or "nameerror" in low
                or "filenotfounderror" in low
                or "indexerror" in low
                or "keyerror" in low
                or "valueerror" in low
                or "runtimeerror" in low
            ):
                important.append(text)
                capture_traceback = "traceback" in low
            elif capture_traceback and (text.startswith("[STDERR]") or text.startswith(" ") or text.startswith("[PYTHON-EXCEPTION]")):
                important.append(text)

        if not important:
            important = logs[-max_lines:]
        else:
            important = important[-max_lines:]

        cleaned: List[str] = []
        for line in important:
            if len(line) > 800:
                line = line[:800] + " ..."
            cleaned.append(line)
        return cleaned

    def _append_child_failure_to_parent(self, parent_id: str, child_id: str, label: str):
        child_task = self.get_task(child_id) or {}
        status = child_task.get("status")
        return_code = child_task.get("return_code")
        self.append_log(
            parent_id,
            f"[CHILD-FAILED] {label} 失败；子任务ID={child_id}；状态={status}；return_code={return_code}",
        )
        cmd = child_task.get("command") or []
        if cmd:
            try:
                self.append_log(parent_id, "[CHILD-COMMAND] " + " ".join(str(x) for x in cmd))
            except Exception:
                pass
        for line in self._extract_error_lines_for_parent(child_task):
            self.append_log(parent_id, f"[CHILD-LOG] {line}")


    def _append_parallel_adjustment_log(self, task_id: str, inputs: Dict[str, Any] | None):
        inputs = inputs or {}
        if not inputs.get("_parallel_auto_adjusted"):
            return
        requested = inputs.get("_requested_parallel_workers") or inputs.get("parallel_workers") or "-"
        effective = inputs.get("_effective_parallel_workers") or inputs.get("_parallel_workers") or "-"
        reason = inputs.get("_parallel_adjust_reason") or "系统负载保护"
        self.append_log(
            task_id,
            f"[SAFE] 用户选择 {requested} 个进程，系统已自动降为 {effective} 个进程。原因：{reason}。",
        )

    def update_task(self, task_id: str, **kwargs):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.update(kwargs)
        self._save_tasks()

    def submit_module_task(
            self,
            module_id: str,
            module_name: str,
            command: List[str],
            inputs: Dict[str, Any],
            working_dir: str | None = None,
            env: Dict[str, str] | None = None,
            owner_username: str = "",
    ) -> Dict[str, Any]:
        task = self.create_task(
            module_id=module_id,
            module_name=module_name,
            command=command,
            inputs=inputs,
            kind="module",
            extra={"owner_username": str(owner_username or "")},
        )

        self._append_parallel_adjustment_log(task["id"], inputs)
        # 单个模块进程只占 1 个真实启动槽；parallel_workers 只写入配置，不作为父任务排队条件。
        self._enqueue_task_runner(
            task["id"],
            self._run_process_task,
            (task["id"], command, working_dir, env),
            requested_slots=1,
        )
        return self.get_task(task["id"]) or task

    def submit_parallel_module_task(
            self,
            module_id: str,
            module_name: str,
            jobs: List[Dict[str, Any]],
            inputs: Dict[str, Any],
            max_workers: int = 2,
            owner_username: str = "",
    ) -> Dict[str, Any]:
        requested_workers = max(1, int(max_workers or 1))
        job_count = len(jobs)
        effective_workers = max(1, min(requested_workers, max(1, job_count)))

        cleanup_roots = sorted({
            str(job.get("cleanup_root") or "")
            for job in jobs
            if str(job.get("cleanup_root") or "").strip()
        })
        input_link_modes = sorted({
            str(mode)
            for job in jobs
            for mode in (job.get("link_modes") or [])
            if str(mode).strip()
        })

        parent_inputs = dict(inputs or {})
        if cleanup_roots:
            parent_inputs["_parallel_cleanup_roots"] = cleanup_roots
        if input_link_modes:
            parent_inputs["_parallel_chunk_link_modes"] = input_link_modes
        parent_inputs["parallel_workers"] = effective_workers
        parent_inputs["_parallel_workers"] = effective_workers
        parent_inputs["_requested_parallel_workers"] = requested_workers
        parent_inputs["_effective_parallel_workers"] = effective_workers
        if requested_workers != effective_workers:
            parent_inputs["_parallel_worker_note"] = f"用户选择 {requested_workers} 个进程，但本次只有 {job_count} 个子任务，实际只申请 {effective_workers} 个 CPU 进程槽。"

        parent = self.create_task(
            module_id=module_id,
            module_name=module_name,
            command=[],
            inputs=parent_inputs,
            kind="parallel",
            extra={
                "parallel_total": job_count,
                "parallel_done": 0,
                "parallel_failed": 0,
                "max_workers": effective_workers,
                "requested_workers": requested_workers,
                "owner_username": str(owner_username or ""),
                "cleanup_roots": cleanup_roots,
            },
        )

        if requested_workers != effective_workers:
            self.append_log(parent["id"], parent_inputs["_parallel_worker_note"])
        self._append_parallel_adjustment_log(parent["id"], parent_inputs)
        self._enqueue_task_runner(
            parent["id"],
            self._run_parallel_task,
            (parent["id"], jobs, effective_workers),
            requested_slots=effective_workers,
        )
        return self.get_task(parent["id"]) or parent

    def submit_batch_group(
            self,
            module_id: str,
            module_name: str,
            jobs: List[Dict[str, Any]],
            max_parallel: int,
            owner_username: str = "",
    ) -> Dict[str, Any]:
        """Submit a batch parent task using a real stable process-pool style.

        Each job becomes one hidden child task. The parent task is the only task shown
        in task management. max_parallel controls how many child processes are allowed
        to run at the same time. The parent must request the same number of CPU slots
        as the real child-process concurrency, otherwise the scheduler will say it only
        obtained 1 slot while the batch group actually launches multiple children.
        """
        requested_parallel = max(1, int(max_parallel or 1))
        job_count = len(jobs)
        effective_parallel = max(1, min(requested_parallel, max(1, job_count)))

        parent_inputs: Dict[str, Any] = {
            "job_count": job_count,
            "parallel_workers": effective_parallel,
            "_parallel_workers": effective_parallel,
            "_requested_parallel_workers": requested_parallel,
            "_effective_parallel_workers": effective_parallel,
        }
        batch_fast_refill = str(os.environ.get("LOCAL_WEB_BATCH_FAST_REFILL", "1")).strip().lower() not in {"0", "false", "no", "off"}
        if batch_fast_refill:
            parent_inputs["_parallel_fast_refill"] = True
        first_input_profiles = next((job.get("input_profiles") for job in jobs if job.get("input_profiles")), None)
        if first_input_profiles:
            parent_inputs["_batch_tif_profiles"] = list(first_input_profiles)
        if requested_parallel != effective_parallel:
            parent_inputs["_parallel_worker_note"] = (
                f"用户选择 {requested_parallel} 个进程，但本次只有 {job_count} 个子任务，"
                f"实际只申请 {effective_parallel} 个 CPU 进程槽。"
            )

        parent = self.create_task(
            module_id=module_id,
            module_name=f"{module_name} 批处理",
            command=[],
            inputs=parent_inputs,
            kind="batch_parent",
            extra={
                "parallel_total": job_count,
                "parallel_done": 0,
                "parallel_failed": 0,
                "max_workers": effective_parallel,
                "requested_workers": requested_parallel,
                "owner_username": str(owner_username or ""),
            },
        )

        child_ids: list[str] = []
        child_job_map: dict[str, Dict[str, Any]] = {}

        for idx, job in enumerate(jobs, start=1):
            child = self.create_task(
                module_id=module_id,
                module_name=f"{module_name} [{idx}/{job_count}]",
                command=job["command"],
                inputs=job["inputs"],
                kind="module",
                extra={
                    "parent_id": parent["id"],
                    "job_index": idx,
                    "owner_username": str(owner_username or ""),
                },
                auto_save=False,
            )
            child_ids.append(child["id"])
            child_job_map[child["id"]] = job

        with self.lock:
            self.tasks[parent["id"]]["children"] = child_ids
            self.tasks[parent["id"]]["status"] = "queued"
        self._save_tasks()

        if parent_inputs.get("_parallel_worker_note"):
            self.append_log(parent["id"], str(parent_inputs["_parallel_worker_note"]))

        first_job_inputs = next(iter(child_job_map.values()), {}).get("inputs") if child_job_map else None
        self._append_parallel_adjustment_log(parent["id"], first_job_inputs)

        self._enqueue_task_runner(
            parent["id"],
            self._run_batch_group,
            (parent["id"], child_job_map, effective_parallel),
            requested_slots=effective_parallel,
        )
        return self.get_task(parent["id"]) or parent

    def _run_batch_group(
        self,
        parent_id: str,
        child_job_map: Dict[str, Dict[str, Any]],
        max_parallel: int,
    ):
        total = len(child_job_map)
        max_parallel = max(1, int(max_parallel or 1))
        self.update_task(
            parent_id,
            status="running",
            started_at=now_iso(),
            parallel_total=total,
            parallel_done=0,
            parallel_failed=0,
            max_workers=max_parallel,
        )
        parent_task = self.get_task(parent_id) or {}
        parent_inputs = parent_task.get("inputs") or {}
        fast_refill = bool(parent_inputs.get("_parallel_fast_refill"))
        self.append_log(parent_id, f"[INFO] 批处理开始，共 {total} 个子任务")
        self.append_log(parent_id, f"[INFO] 用户选择并发数 = {max_parallel}；系统会逐个启动子进程，负载高时暂停启动新进程")
        if fast_refill:
            self.append_log(parent_id, "[POOL] 批处理快速补位已启用：一个影像子任务完成后立即补下一个，不使用自适应错峰等待。")
        input_profiles = parent_inputs.get("_batch_tif_profiles") or []
        if input_profiles:
            self.append_log(parent_id, "[DATA] 首个批处理子任务 TIF 输入结构如下：")
            for item in input_profiles:
                self.append_log(parent_id, f"[DATA]   {item}")
        self.append_log(parent_id, "[POOL] 稳定进程池：最多同时运行 max_parallel 个子任务；一个完成后补一个；检测到高负载时先等已有子任务完成，再决定是否补位。")
        self.append_log(parent_id, "[SAFE] 启动新子进程前检查 CPU/内存/磁盘，真正接近危险阈值时才暂停补位。")

        job_items = list(child_job_map.items())
        child_label_map: Dict[str, str] = {
            child_id: str((job or {}).get("label") or child_id)
            for child_id, job in job_items
        }
        next_index = 0
        failures = 0
        done = 0
        with self.lock:
            start_gate = self.child_start_gate_locks.setdefault(parent_id, threading.Lock())
        learned_interval = self.learned_child_start_intervals.get(parent_id)
        last_child_launch_at = 0.0

        def _worker(child_id: str, job: Dict[str, Any]):
            child_snapshot = self.get_task(child_id) or {}
            if parent_id in self.cancel_flags or child_snapshot.get("status") == "cancelled":
                self.update_task(child_id, status="cancelled", ended_at=now_iso())
                return child_id
            self._run_process_task(
                child_id,
                job["command"],
                job.get("working_dir"),
                job.get("env"),
            )
            return child_id

        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            running: Dict[Any, str] = {}
            paused_by_pressure = False
            while (next_index < total or running) and parent_id not in self.cancel_flags:
                launched_any = False
                while next_index < total and len(running) < max_parallel and parent_id not in self.cancel_flags:
                    child_id, job = job_items[next_index]
                    label = job.get("label") or child_id

                    # 一旦检测到 CPU/内存/磁盘压力，就不要在没有子任务完成的情况下继续补位。
                    # 这样不会出现“刚提示暂停，马上又把下一个任务提交上去”的情况。
                    if paused_by_pressure and running:
                        break

                    reason = self._runtime_pressure_reason(fast_refill=fast_refill)
                    if reason:
                        paused_by_pressure = True
                        now = time.time()
                        last = self._last_pressure_log_at.get(parent_id, 0.0)
                        if now - last >= 8:
                            self._last_pressure_log_at[parent_id] = now
                            self.append_log(
                                parent_id,
                                f"[SAFE] 暂停启动新子任务 {label}：{reason}。当前运行 {len(running)} 个；已完成 {done}/{total}。",
                            )
                        break

                    with start_gate:
                        self._wait_until_safe_to_start_child(parent_id, str(label), fast_refill=fast_refill)
                        if parent_id in self.cancel_flags:
                            break
                        if self.adaptive_child_start_enabled and not fast_refill and learned_interval is not None:
                            if not self._sleep_before_adaptive_child_launch(parent_id, str(label), learned_interval, last_child_launch_at):
                                break
                            self._wait_until_safe_to_start_child(parent_id, str(label), fast_refill=fast_refill)
                            if parent_id in self.cancel_flags:
                                break

                        self.append_log(parent_id, f"[INFO] 启动子任务 {next_index + 1}/{total}: {label}；当前运行 {len(running) + 1}/{max_parallel}")
                        future = executor.submit(_worker, child_id, job)
                        running[future] = child_id
                        next_index += 1
                        launched_any = True
                        last_child_launch_at = time.time()

                    if self.adaptive_child_start_enabled and not fast_refill and learned_interval is None and next_index < total:
                        learned_interval = self._learn_child_start_interval_after_first_launch(parent_id, str(label))
                        self.learned_child_start_intervals[parent_id] = learned_interval
                    elif self.child_start_stagger_seconds > 0 and not fast_refill:
                        time.sleep(self.child_start_stagger_seconds)

                if not running:
                    # 没有运行中的子任务时，即使压力高也需要周期性重试。
                    paused_by_pressure = False
                    time.sleep(max(1.0, self.child_launch_wait_seconds))
                    continue

                done_set, _ = wait(set(running.keys()), timeout=1.0, return_when=FIRST_COMPLETED)
                if not done_set and not launched_any:
                    time.sleep(0.5)
                    continue

                if done_set:
                    paused_by_pressure = False

                for future in done_set:
                    child_id = running.pop(future, "")
                    try:
                        future.result()
                        task = self.get_task(child_id) or {}
                        status = task.get("status")
                        return_code = task.get("return_code")
                        if status != "success":
                            failures += 1
                            self._append_child_failure_to_parent(
                                parent_id,
                                child_id,
                                child_label_map.get(child_id, child_id),
                            )
                        self.append_log(parent_id, f"[INFO] 子任务完成: {child_id}, 状态={status}, return_code={return_code}")
                    except Exception as e:
                        failures += 1
                        self.append_log(parent_id, f"[ERROR] 子任务异常: {child_id} -> {repr(e)}")
                        self.append_log(parent_id, traceback.format_exc())
                        if child_id:
                            self._append_child_failure_to_parent(
                                parent_id,
                                child_id,
                                child_label_map.get(child_id, child_id),
                            )

                    done += 1
                    self.update_task(parent_id, parallel_done=done, parallel_failed=failures)

        if parent_id in self.cancel_flags:
            final_status = "cancelled"
            return_code = -1
        else:
            final_status = "success" if failures == 0 and done == total else "failed"
            return_code = 0 if final_status == "success" else 1

        self.update_task(
            parent_id,
            status=final_status,
            ended_at=now_iso(),
            return_code=return_code,
            parallel_done=done,
            parallel_failed=failures,
        )
        self.append_log(parent_id, f"[INFO] 批处理结束，完成={done}/{total}，失败数={failures}")
        with self.lock:
            self.learned_child_start_intervals.pop(parent_id, None)
            self.child_start_gate_locks.pop(parent_id, None)
            self._last_parallel_monitor_log_at.pop(parent_id, None)
            self._last_dynamic_boost_log_at.pop(parent_id, None)
        self.cancel_flags.discard(parent_id)

    def _stream_reader(self, pipe, task_id: str, prefix: str):
        try:
            if pipe is None:
                return

            for raw in iter(pipe.readline, b""):
                if not raw:
                    break

                line = self.decode_process_output(raw).rstrip("\r\n")

                if line:
                    self.append_log(task_id, f"[{prefix}] {line}")

        except Exception as e:
            self.append_log(task_id, f"[PYTHON-LOG-ERROR] {prefix}: {repr(e)}")
        finally:
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass

    def _log_runtime_context(
            self,
            task_id: str,
            command: List[str],
            working_dir: str | None,
            merged_env: Dict[str, str],
    ):
        self.append_log(task_id, "[INFO] 准备启动模块")
        self.append_log(task_id, f"[INFO] cwd = {working_dir or os.getcwd()}")
        self.append_log(task_id, f"[INFO] command = {' '.join(command)}")

        runtime_source_mode = merged_env.get("RUNTIME_SOURCE_MODE", "")
        fixed_resource_policy = merged_env.get("RUNTIME_FIXED_RESOURCE_POLICY", "")
        if runtime_source_mode or fixed_resource_policy:
            self.append_log(
                task_id,
                f"[INFO] runtime_source_mode = {runtime_source_mode or '-'}；fixed_resource_policy = {fixed_resource_policy or '-'}",
            )
        if merged_env.get("LOCAL_WEB_NO_FIXED_RESOURCE_COPY") == "1":
            self.append_log(
                task_id,
                "[INFO] 固定资源不复制：模型、pkl、resources、LUT 等固定文件直接从 installed_modules 模块目录读取；本任务只生成独立 config.json。",
            )
            if merged_env.get("RUNTIME_SHARED_SOURCE_DIR"):
                self.append_log(task_id, f"[INFO] 固定资源读取目录 = {merged_env.get('RUNTIME_SHARED_SOURCE_DIR')}")
            if merged_env.get("RUNTIME_CONFIG_ONLY_DIR"):
                self.append_log(task_id, f"[INFO] 本任务配置目录 = {merged_env.get('RUNTIME_CONFIG_ONLY_DIR')}")

        path_value = merged_env.get("PATH", "")
        path_parts = path_value.split(";") if path_value else []
        self.append_log(task_id, "[INFO] PATH 前 10 项如下：")
        for idx, item in enumerate(path_parts[:10], start=1):
            self.append_log(task_id, f"[INFO]   {idx}. {item}")

        self.append_log(
            task_id,
            f"[INFO] OPENBLAS_NUM_THREADS = {merged_env.get('OPENBLAS_NUM_THREADS', '')}",
        )
        self.append_log(
            task_id,
            f"[INFO] OMP_NUM_THREADS = {merged_env.get('OMP_NUM_THREADS', '')}",
        )
        self.append_log(
            task_id,
            f"[INFO] GOTO_NUM_THREADS = {merged_env.get('GOTO_NUM_THREADS', '')}",
        )
        self.append_log(
            task_id,
            f"[INFO] MKL_NUM_THREADS = {merged_env.get('MKL_NUM_THREADS', '')}",
        )
        self.append_log(
            task_id,
            f"[INFO] LOKY_MAX_CPU_COUNT = {merged_env.get('LOKY_MAX_CPU_COUNT', '')}",
        )
        self.append_log(
            task_id,
            f"[INFO] LOCAL_WEB_RUNTIME_THREADS = {merged_env.get('LOCAL_WEB_RUNTIME_THREADS', '')}",
        )

        config_arg = None

        for item in reversed(command):
            try:
                p = Path(str(item))
                if p.suffix.lower() == ".json":
                    config_arg = p
                    break
            except Exception:
                pass

        if not config_arg:
            return

        self.append_log(task_id, f"[INFO] config/input = {config_arg}")

        if config_arg.exists() and config_arg.suffix.lower() == ".json":
            try:
                content = config_arg.read_text(encoding="utf-8")
                self.append_log(task_id, "[INFO] config.json 内容如下：")
                for line in content.splitlines():
                    self.append_log(task_id, line)
            except Exception as e:
                self.append_log(task_id, f"[WARN] 读取 config.json 失败: {repr(e)}")

    def _hint_from_return_code(self, return_code: int) -> Optional[str]:
        if return_code == 0:
            return None

        hints = {
            -1073741502: "对应 0xc0000142，通常是 DLL / 运行库初始化失败。",
            3221225794: "通常对应 0xc0000142，常见于 DLL 初始化失败。",
            -1073741515: "通常是缺少依赖 DLL。",
            -1073740791: "通常表示原生程序崩溃或堆损坏。",
            -1073741819: "通常表示访问冲突（0xC0000005）。",
        }
        return hints.get(return_code)

    @staticmethod
    def _logs_contain_failure_signature(logs: Any) -> bool:
        if not logs:
            return False
        if isinstance(logs, str):
            text = logs
        else:
            try:
                text = "\n".join(str(line) for line in logs)
            except Exception:
                text = str(logs)
        lowered = text.lower()
        signatures = (
            "no such file",
            "no such directory",
            "file not found",
            "path not found",
            "找不到文件",
            "系统找不到指定的文件",
        )
        return any(item in lowered for item in signatures)

    @staticmethod
    def decode_process_output(raw: bytes) -> str:
        if raw is None:
            return ""

        for encoding in ("utf-8", "gbk", "cp936"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue

        return raw.decode("utf-8", errors="replace")
    def _run_process_task(
        self,
        task_id: str,
        command: List[str],
        working_dir: str | None,
        env: Dict[str, str] | None,
    ):
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        affinity_cores: List[int] = []
        process: subprocess.Popen | None = None

        try:
            # 先申请 CPU 核组，再写入环境变量和启动进程。
            # 这样每个 exe 从启动开始就能获得明确的线程预算。
            affinity_cores = self._acquire_cpu_affinity_group(task_id)
            if affinity_cores:
                self._inject_affinity_thread_env(merged_env, affinity_cores)

            self._log_runtime_context(task_id, command, working_dir, merged_env)

            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW

            process = subprocess.Popen(
                command,
                cwd=working_dir or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=False,
                bufsize=0,
                env=merged_env,
                creationflags=creationflags,
            )

            affinity_ok = False
            affinity_error = ""
            if affinity_cores:
                affinity_ok, affinity_error = self._apply_cpu_affinity_to_process(process.pid, affinity_cores)

            with self.lock:
                self.processes[task_id] = process
                task = self.tasks.get(task_id)
                if task:
                    task["status"] = "running"
                    task["pid"] = process.pid
                    task["started_at"] = now_iso()
                    if affinity_cores:
                        task["cpu_affinity_cores"] = list(affinity_cores)
                        task["cpu_affinity_label"] = self._format_core_range(affinity_cores)
                        task["runtime_threads"] = int(merged_env.get("LOCAL_WEB_RUNTIME_THREADS") or len(affinity_cores))
                    task.setdefault("logs", []).append(
                        f"[INFO] 进程已启动，PID = {process.pid}"
                    )
                    if affinity_cores:
                        label = self._format_core_range(affinity_cores)
                        threads = merged_env.get("LOCAL_WEB_RUNTIME_THREADS") or str(len(affinity_cores))
                        if affinity_ok:
                            task.setdefault("logs", []).append(
                                f"[AFFINITY] 已绑定 CPU 核组：{label}；每进程线程数限制为 {threads}。"
                            )
                        else:
                            task.setdefault("logs", []).append(
                                f"[AFFINITY-WARN] 已分配 CPU 核组 {label}，但设置进程亲和性失败：{affinity_error}"
                            )
                    elif self.cpu_affinity_enabled:
                        task.setdefault("logs", []).append(
                            "[AFFINITY-WARN] CPU 亲和性已启用，但没有可用核组，本进程不绑定 CPU。"
                        )
            self._save_tasks()

            t_out = threading.Thread(
                target=self._stream_reader,
                args=(process.stdout, task_id, "STDOUT"),
                daemon=True,
            )
            t_err = threading.Thread(
                target=self._stream_reader,
                args=(process.stderr, task_id, "STDERR"),
                daemon=True,
            )
            t_out.start()
            t_err.start()

            return_code = process.wait()

            t_out.join(timeout=1)
            t_err.join(timeout=1)

            with self.lock:
                task = self.tasks.get(task_id)
                if task:
                    failure_signature = self._logs_contain_failure_signature(task.get("logs") or [])
                    if task.get("status") != "cancelled":
                        task["return_code"] = return_code
                        task["status"] = "failed" if return_code != 0 or failure_signature else "success"
                        task["ended_at"] = now_iso()
                    task.setdefault("logs", []).append(
                        f"[INFO] 进程结束，return_code = {return_code}"
                    )
                    if failure_signature and return_code == 0:
                        task.setdefault("logs", []).append(
                            "[OUTPUT-ERROR] 子程序返回 0，但日志包含找不到文件/路径的失败信息，平台已按失败处理。"
                        )

                    hint = self._hint_from_return_code(return_code)
                    if hint:
                        task.setdefault("logs", []).append(f"[HINT] {hint}")

                    if return_code != 0 and not task.get("logs"):
                        task.setdefault("logs", []).append(
                            "[WARN] 进程失败，但没有捕获到 stdout/stderr。"
                        )

            self._save_tasks()

        except Exception as e:
            with self.lock:
                task = self.tasks.get(task_id)
                if task:
                    if task.get("status") != "cancelled":
                        task["status"] = "failed"
                        task["return_code"] = -1
                        task["ended_at"] = now_iso()
                    task.setdefault("logs", []).append(
                        f"[PYTHON-EXCEPTION] {repr(e)}"
                    )
                    task.setdefault("logs", []).append(traceback.format_exc())
            self._save_tasks()

        finally:
            with self.lock:
                self.processes.pop(task_id, None)
            self._release_cpu_affinity_group(task_id)
            self._save_tasks()

    def _run_parallel_tile_merge(self, parent_id: str) -> bool:
        parent = self.get_task(parent_id) or {}
        inputs = parent.get("inputs") or {}
        merge_plan = inputs.get("_parallel_tile_merge_plan")
        if not merge_plan:
            return True

        plans = merge_plan.get("plans") if isinstance(merge_plan, dict) else None
        if not isinstance(plans, list) or not plans:
            plans = [merge_plan]

        try:
            for idx, plan in enumerate(plans, start=1):
                if not isinstance(plan, dict):
                    continue
                tile_count = int(plan.get("tile_count") or len(plan.get("tiles") or []))
                job_count = int(plan.get("job_count") or 0)
                grouped = bool(plan.get("grouped_for_model_reuse"))
                self.append_log(
                    parent_id,
                    f"[TILE] 开始拼接瓦片 {idx}/{len(plans)}：tiles={tile_count}，jobs={job_count}，模型复用分组={'是' if grouped else '否'}",
                )
                output_path = merge_tif_tiles(
                    plan,
                    log=lambda message: self.append_log(parent_id, message),
                )
                self.append_log(parent_id, f"[TILE] 瓦片拼接完成：{output_path}")
            return True
        except Exception as exc:
            self.append_log(parent_id, f"[TILE-ERROR] 瓦片拼接失败：{type(exc).__name__}: {exc}")
            self.append_log(parent_id, traceback.format_exc())
            return False

    def _prepare_parallel_output_progress(self, jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
        dirs: list[Path] = []
        extensions: set[str] = set()
        inputs: list[dict] = []
        seen_dirs: set[str] = set()
        seen_input_ids: set[str] = set()
        require_outputs = False

        for job in jobs:
            if job.get("require_outputs") or (job.get("inputs") or {}).get("_parallel_require_outputs"):
                require_outputs = True
            watch = job.get("progress_watch") or {}
            for raw_dir in watch.get("output_dirs") or []:
                try:
                    path = Path(str(raw_dir)).resolve()
                except Exception:
                    continue
                key = str(path).lower()
                if key not in seen_dirs:
                    seen_dirs.add(key)
                    dirs.append(path)

            for ext in watch.get("output_extensions") or []:
                ext_text = str(ext or "").strip().lower()
                if not ext_text:
                    continue
                if not ext_text.startswith("."):
                    ext_text = "." + ext_text
                extensions.add(ext_text)

            for item in watch.get("inputs") or []:
                input_id = str(item.get("id") or "").strip()
                if not input_id or input_id in seen_input_ids:
                    continue
                tokens = [
                    str(token or "").strip().lower()
                    for token in (item.get("tokens") or [])
                    if str(token or "").strip()
                ]
                if not tokens:
                    continue
                seen_input_ids.add(input_id)
                inputs.append({"id": input_id, "tokens": sorted(set(tokens))})

        if not dirs or not inputs:
            return {"enabled": False, "require_outputs": require_outputs}

        if not extensions:
            extensions = {".tif", ".tiff"}

        baseline: dict[str, tuple[int, int]] = {}
        for directory in dirs:
            if not directory.exists() or not directory.is_dir():
                continue
            try:
                iterator = directory.rglob("*") if len(inputs) <= 200 else directory.glob("*")
                for path in iterator:
                    if not path.is_file() or path.suffix.lower() not in extensions:
                        continue
                    try:
                        stat = path.stat()
                        baseline[str(path.resolve()).lower()] = (int(stat.st_mtime_ns), int(stat.st_size))
                    except Exception:
                        continue
            except Exception:
                continue

        return {
            "enabled": True,
            "dirs": dirs,
            "extensions": extensions,
            "inputs": inputs,
            "baseline": baseline,
            "seen_ids": set(),
            "seen_output_keys": set(),
            "require_outputs": require_outputs,
            "last_scan_at": 0.0,
            "last_report_count": 0,
        }

    def _scan_parallel_output_progress(self, state: Dict[str, Any], force: bool = False) -> list[str]:
        if not state.get("enabled"):
            return []

        now = time.time()
        if not force and now - float(state.get("last_scan_at") or 0.0) < max(0.2, self.parallel_progress_scan_interval_seconds):
            return []
        state["last_scan_at"] = now

        extensions = state.get("extensions") or {".tif", ".tiff"}
        baseline = state.get("baseline") or {}
        seen_ids: set[str] = state.setdefault("seen_ids", set())
        seen_output_keys: set[str] = state.setdefault("seen_output_keys", set())
        matched: list[str] = []
        unmatched_outputs: list[tuple[str, str]] = []

        candidates: list[Path] = []
        for directory in state.get("dirs") or []:
            try:
                path = Path(directory)
                if not path.exists() or not path.is_dir():
                    continue
                iterator = path.rglob("*") if len(state.get("inputs") or []) <= 200 else path.glob("*")
                for item in iterator:
                    if item.is_file() and item.suffix.lower() in extensions:
                        candidates.append(item)
            except Exception:
                continue

        for path in candidates:
            try:
                stat = path.stat()
                key = str(path.resolve()).lower()
                snapshot = (int(stat.st_mtime_ns), int(stat.st_size))
            except Exception:
                continue
            if baseline.get(key) == snapshot:
                continue
            if key in seen_output_keys:
                continue
            name = path.name.lower()
            stem = path.stem.lower()
            matched_by_token = False
            for item in state.get("inputs") or []:
                input_id = str(item.get("id") or "")
                if not input_id or input_id in seen_ids:
                    continue
                tokens = item.get("tokens") or []
                if any(token and (token in name or token in stem) for token in tokens):
                    seen_ids.add(input_id)
                    seen_output_keys.add(key)
                    matched.append(input_id)
                    matched_by_token = True
                    break
            if not matched_by_token:
                unmatched_outputs.append((key, path.name))

        if unmatched_outputs:
            inputs = state.get("inputs") or []
            for key, output_name in unmatched_outputs:
                if key in seen_output_keys:
                    continue
                target = next(
                    (
                        str(item.get("id") or "")
                        for item in inputs
                        if str(item.get("id") or "") and str(item.get("id") or "") not in seen_ids
                    ),
                    "",
                )
                if not target:
                    break
                seen_ids.add(target)
                seen_output_keys.add(key)
                matched.append(target or output_name)

        return matched

    def _apply_parallel_output_progress(
        self,
        parent_id: str,
        progress: Dict[str, int],
        progress_total: int,
        progress_label: str,
        state: Dict[str, Any],
        force_log: bool = False,
    ) -> None:
        if not state.get("enabled"):
            return

        matched = self._scan_parallel_output_progress(state, force=force_log)
        seen_count = min(progress_total, len(state.get("seen_ids") or set()))
        if seen_count <= int(progress.get("done") or 0):
            return

        progress["done"] = seen_count
        self.update_task(parent_id, parallel_done=progress["done"], parallel_failed=progress["failed"])

        last_report = int(state.get("last_report_count") or 0)
        if force_log or matched or seen_count > last_report:
            state["last_report_count"] = seen_count
            if matched:
                names = ", ".join(matched[-3:])
                extra = f"；最新：{names}"
            else:
                extra = ""
            self.append_log(parent_id, f"[PROGRESS] 已输出 {seen_count}/{progress_total} 个{progress_label}{extra}")

    def _parallel_monitor_snapshot(self) -> Dict[str, Any]:
        mem = self._virtual_memory_snapshot()
        return {
            "cpu": self._system_cpu_percent(),
            "memory_percent": mem.get("percent"),
            "memory_available_gb": mem.get("available_gb"),
        }

    def _dynamic_parallel_worker_limit(
        self,
        parent_id: str,
        base_workers: int,
        pool_capacity: int,
        running_count: int,
        pending_count: int,
    ) -> int:
        if (
            not self.dynamic_worker_boost_enabled
            or pending_count <= 0
            or pool_capacity <= base_workers
            or running_count < base_workers
        ):
            return base_workers

        snapshot = self._parallel_monitor_snapshot()
        cpu = snapshot.get("cpu")
        mem_percent = snapshot.get("memory_percent")
        mem_available = snapshot.get("memory_available_gb")
        safe = True
        reasons: list[str] = []

        if cpu is not None and cpu > self.dynamic_worker_boost_cpu_below:
            safe = False
            reasons.append(f"CPU {cpu:.1f}%")
        if mem_percent is not None and mem_percent >= self.dynamic_worker_boost_memory_below:
            safe = False
            reasons.append(f"内存 {mem_percent:.1f}%")
        if mem_available is not None and mem_available < self.dynamic_worker_boost_min_memory_gb:
            safe = False
            reasons.append(f"可用内存 {mem_available:.1f}GB")

        now = time.time()
        last = self._last_dynamic_boost_log_at.get(parent_id, 0.0)
        if not safe:
            if now - last >= 20:
                self._last_dynamic_boost_log_at[parent_id] = now
                detail = "，".join(reasons) if reasons else "资源状态未满足"
                self.append_log(parent_id, f"[BOOST] 暂不增加临时 worker：{detail}")
            return base_workers

        limit = min(pool_capacity, base_workers + self.dynamic_worker_boost_extra)
        if now - last >= 20:
            self._last_dynamic_boost_log_at[parent_id] = now
            cpu_text = "-" if cpu is None else f"{cpu:.1f}%"
            mem_text = "-" if mem_percent is None else f"{mem_percent:.1f}%"
            avail_text = "-" if mem_available is None else f"{mem_available:.1f}GB"
            self.append_log(parent_id, f"[BOOST] CPU/内存允许，临时并发上限提高到 {limit}：CPU={cpu_text}，内存={mem_text}，可用={avail_text}")
        return limit

    def _maybe_log_parallel_monitor(self, parent_id: str, running_count: int, base_workers: int, pool_capacity: int) -> None:
        now = time.time()
        last = self._last_parallel_monitor_log_at.get(parent_id, 0.0)
        if now - last < max(5.0, self.parallel_monitor_interval_seconds):
            return
        self._last_parallel_monitor_log_at[parent_id] = now
        snapshot = self._parallel_monitor_snapshot()
        cpu = snapshot.get("cpu")
        mem_percent = snapshot.get("memory_percent")
        mem_available = snapshot.get("memory_available_gb")
        cpu_text = "-" if cpu is None else f"{cpu:.1f}%"
        mem_text = "-" if mem_percent is None else f"{mem_percent:.1f}%"
        avail_text = "-" if mem_available is None else f"{mem_available:.1f}GB"
        self.append_log(
            parent_id,
            f"[MONITOR] CPU={cpu_text}，内存={mem_text}，可用内存={avail_text}，运行 worker={running_count}/{base_workers}，池容量={pool_capacity}",
        )

    def _run_parallel_task(self, parent_id: str, jobs: List[Dict[str, Any]], max_workers: int):
        total = len(jobs)
        max_workers = max(1, min(int(max_workers or 1), max(1, total)))
        parent_task = self.get_task(parent_id) or {}
        parent_inputs = parent_task.get("inputs") or {}

        def _job_progress_units(job: Dict[str, Any]) -> int:
            inputs = job.get("inputs") or {}
            for key in ("_parallel_chunk_file_count", "_parallel_tile_count"):
                try:
                    value = int(inputs.get(key) or 0)
                    if value > 0:
                        return value
                except Exception:
                    pass
            return 1

        progress_total = sum(_job_progress_units(job) for job in jobs) or total
        progress_unit = "files" if progress_total != total else "jobs"
        progress_label = "输入文件" if progress_unit == "files" else "子任务"

        self.update_task(
            parent_id,
            status="running",
            started_at=now_iso(),
            parallel_total=progress_total,
            parallel_done=0,
            parallel_failed=0,
            parallel_progress_unit=progress_unit,
            parallel_progress_label=progress_label,
            max_workers=max_workers,
        )

        self.append_log(parent_id, f"[PARALLEL] 并行任务启动：输入文件数={progress_total}，分组数={total}，实际并行 worker={max_workers}")
        fast_refill = bool(parent_inputs.get("_parallel_fast_refill"))
        link_modes = parent_inputs.get("_parallel_chunk_link_modes") or []
        if link_modes:
            self.append_log(
                parent_id,
                f"[LINK] 子任务输入文件引用方式：{', '.join(str(x) for x in link_modes)}；系统未复制原始输入大文件到 runtime。"
            )
        if fast_refill:
            self.append_log(parent_id, "[POOL] 快速补位已启用：子任务完成后立即补下一个，不使用自适应错峰等待。")
        model_reuse_info = parent_inputs.get("_parallel_model_reuse_info") or {}
        if model_reuse_info:
            self.append_log(
                parent_id,
                (
                    "[MODEL] 大模型复用分组已启用："
                    f"原计划约 {model_reuse_info.get('original_job_count', '-')} 个子进程，"
                    f"现在合并为 {model_reuse_info.get('grouped_job_count', total)} 个文件组；"
                    f"同时运行 {max_workers} 个 worker；"
                    f"固定模型/资源约 {float(model_reuse_info.get('fixed_resource_gb') or 0.0):.2f}GB。"
                ),
            )
            thread_counts: List[int] = []
            for spec in jobs[:max_workers]:
                env = spec.get("env") or {}
                raw_threads = (
                    env.get("LOCAL_WEB_RUNTIME_THREADS")
                    or env.get("OMP_NUM_THREADS")
                    or env.get("OPENBLAS_NUM_THREADS")
                )
                try:
                    thread_counts.append(max(1, int(raw_threads or 1)))
                except Exception:
                    pass
            if thread_counts:
                unique_threads = sorted(set(thread_counts))
                thread_text = (
                    str(unique_threads[0])
                    if len(unique_threads) == 1
                    else ",".join(str(x) for x in unique_threads)
                )
                self.append_log(
                    parent_id,
                    (
                        "[THREAD] 模型复用 worker 内部线程数="
                        f"{thread_text}；首批预计计算线程总数={sum(thread_counts)}。"
                    ),
                )
        self.append_log(parent_id, "[POOL] 稳定进程池：最多同时运行 max_workers 个子任务；一个完成后补一个；检测到高负载时先等已有子任务完成，再决定是否补位。")
        self.append_log(parent_id, "[SAFE] 不再按模型文件大小直接降为 1；启动新子进程前检查 CPU/内存/磁盘，真正接近危险阈值时才暂停补位。")

        if model_reuse_info:
            first_thread_counts: List[int] = []
            first_inner_workers: List[int] = []
            for spec in jobs[:max_workers]:
                env = spec.get("env") or {}
                inputs = spec.get("inputs") or {}
                try:
                    first_thread_counts.append(max(1, int(
                        env.get("LOCAL_WEB_RUNTIME_THREADS")
                        or env.get("OMP_NUM_THREADS")
                        or env.get("OPENBLAS_NUM_THREADS")
                        or 1
                    )))
                except Exception:
                    pass
                try:
                    first_inner_workers.append(max(1, int(inputs.get("parallel_workers") or inputs.get("_parallel_workers") or 1)))
                except Exception:
                    pass
            if first_thread_counts or first_inner_workers:
                thread_values = sorted(set(first_thread_counts or [1]))
                inner_values = sorted(set(first_inner_workers or [1]))
                thread_text = str(thread_values[0]) if len(thread_values) == 1 else ",".join(str(x) for x in thread_values)
                inner_text = str(inner_values[0]) if len(inner_values) == 1 else ",".join(str(x) for x in inner_values)
                self.append_log(
                    parent_id,
                    (
                        "[POOL] 外层并行进程数="
                        f"{max_workers}；子任务 config.parallel_workers={inner_text}；"
                        f"每进程计算线程={thread_text}；预计总计算线程={sum(first_thread_counts or [max_workers])}。"
                    ),
                )

        progress = {"done": 0, "failed": 0}
        progress_state = self._prepare_parallel_output_progress(jobs)
        completed_success_units = 0
        pool_capacity = min(
            total,
            max_workers + (self.dynamic_worker_boost_extra if self.dynamic_worker_boost_enabled else 0),
        )
        pool_capacity = max(max_workers, pool_capacity)
        if progress_state.get("enabled"):
            self.append_log(parent_id, "[PROGRESS] 已启用输出目录实时扫描：输出文件落盘后立即更新输入文件进度。")
        output_driven_progress = bool(progress_state.get("enabled") and progress_state.get("require_outputs"))
        if self.dynamic_worker_boost_enabled and pool_capacity > max_workers:
            self.append_log(
                parent_id,
                (
                    "[BOOST] 动态 CPU 监控已启用："
                    f"CPU 低于 {self.dynamic_worker_boost_cpu_below:.0f}%、"
                    f"内存低于 {self.dynamic_worker_boost_memory_below:.0f}%、"
                    f"可用内存不少于 {self.dynamic_worker_boost_min_memory_gb:.1f}GB 时，"
                    f"临时 worker 上限最多提高到 {pool_capacity}。"
                ),
            )
        with self.lock:
            start_gate = self.child_start_gate_locks.setdefault(parent_id, threading.Lock())
        learned_interval = self.learned_child_start_intervals.get(parent_id)
        last_child_launch_at = 0.0

        def run_one(index: int, spec: Dict[str, Any]):
            if parent_id in self.cancel_flags:
                return None

            label = spec.get("label") or f"子任务 {index + 1}"
            parent_task = self.get_task(parent_id) or {}
            owner_username = str(parent_task.get("owner_username") or "")

            child = self.create_task(
                module_id=spec.get("module_id", ""),
                module_name=spec.get("module_name", label),
                command=spec.get("command") or [],
                inputs=spec.get("inputs") or {},
                kind="module",
                extra={
                    "parent_id": parent_id,
                    "worker_no": None,
                    "job_index": index + 1,
                    "owner_username": owner_username,
                },
            )

            with self.lock:
                parent = self.tasks.get(parent_id)
                if parent:
                    parent.setdefault("children", []).append(child["id"])
            self._save_tasks()

            self.append_log(parent_id, f"[PARALLEL] 启动 {index + 1}/{total}: {label}")

            self._run_process_task(
                child["id"],
                spec.get("command") or [],
                spec.get("working_dir"),
                spec.get("env"),
            )

            child_task = self.get_task(child["id"]) or {}
            return child["id"], child_task.get("status")

        failures = 0
        next_index = 0

        with ThreadPoolExecutor(max_workers=pool_capacity) as executor:
            running: Dict[Any, int] = {}
            paused_by_pressure = False
            while (next_index < total or running) and parent_id not in self.cancel_flags:
                launched_any = False
                self._maybe_log_parallel_monitor(parent_id, len(running), max_workers, pool_capacity)
                self._apply_parallel_output_progress(parent_id, progress, progress_total, progress_label, progress_state)
                desired_workers = self._dynamic_parallel_worker_limit(
                    parent_id,
                    max_workers,
                    pool_capacity,
                    len(running),
                    total - next_index,
                )
                while next_index < total and len(running) < desired_workers and parent_id not in self.cancel_flags:
                    spec = jobs[next_index]
                    label = spec.get("label") or f"子任务 {next_index + 1}"

                    # 一旦检测到 CPU/内存/磁盘压力，就等待至少一个正在运行的子任务结束后再判断是否补位。
                    # 旧版会在压力短暂波动时继续提交，导致日志里出现“暂停后仍提交第 6 个任务”。
                    if paused_by_pressure and running:
                        break

                    reason = self._runtime_pressure_reason(fast_refill=fast_refill)
                    if reason:
                        paused_by_pressure = True
                        now = time.time()
                        last = self._last_pressure_log_at.get(parent_id, 0.0)
                        if now - last >= 8:
                            self._last_pressure_log_at[parent_id] = now
                            self.append_log(
                                parent_id,
                                f"[SAFE] 暂停启动新子任务 {label}：{reason}。当前运行 {len(running)} 个；已完成 {progress['done']}/{progress_total} 个{progress_label}。",
                            )
                        break

                    with start_gate:
                        self._wait_until_safe_to_start_child(parent_id, str(label), fast_refill=fast_refill)
                        if parent_id in self.cancel_flags:
                            break
                        if self.adaptive_child_start_enabled and not fast_refill and learned_interval is not None:
                            if not self._sleep_before_adaptive_child_launch(parent_id, str(label), learned_interval, last_child_launch_at):
                                break
                            self._wait_until_safe_to_start_child(parent_id, str(label), fast_refill=fast_refill)
                            if parent_id in self.cancel_flags:
                                break

                        future = executor.submit(run_one, next_index, spec)
                        running[future] = next_index
                        self.append_log(parent_id, f"[PARALLEL] 已提交分组 {next_index + 1}/{total}；当前运行 {len(running)}/{desired_workers}")
                        next_index += 1
                        launched_any = True
                        last_child_launch_at = time.time()

                    if self.adaptive_child_start_enabled and not fast_refill and learned_interval is None and next_index < total:
                        learned_interval = self._learn_child_start_interval_after_first_launch(parent_id, str(label))
                        self.learned_child_start_intervals[parent_id] = learned_interval
                    elif self.child_start_stagger_seconds > 0 and not fast_refill:
                        time.sleep(self.child_start_stagger_seconds)

                if not running:
                    paused_by_pressure = False
                    time.sleep(max(1.0, self.child_launch_wait_seconds))
                    continue

                self._apply_parallel_output_progress(parent_id, progress, progress_total, progress_label, progress_state)
                done_set, _ = wait(set(running.keys()), timeout=1.0, return_when=FIRST_COMPLETED)
                if not done_set and not launched_any:
                    self._apply_parallel_output_progress(parent_id, progress, progress_total, progress_label, progress_state)
                    time.sleep(0.5)
                    continue

                if done_set:
                    paused_by_pressure = False

                for future in done_set:
                    idx = running.pop(future, -1)
                    label = jobs[idx].get("label") if 0 <= idx < len(jobs) else "子任务"
                    units = _job_progress_units(jobs[idx]) if 0 <= idx < len(jobs) else 1
                    try:
                        result = future.result()
                        child_id, status = result if result else (None, "cancelled")
                        if status != "success":
                            failures += 1
                            if child_id:
                                self._append_child_failure_to_parent(parent_id, child_id, label)
                            progress["failed"] += units
                        else:
                            completed_success_units += units
                            if not output_driven_progress:
                                progress["done"] = max(
                                    progress["done"],
                                    min(progress_total, completed_success_units),
                                )
                        self.update_task(parent_id, parallel_done=progress["done"], parallel_failed=progress["failed"])
                        self.append_log(parent_id, f"[PARALLEL] 完成 {progress['done'] + progress['failed']}/{progress_total} 个{progress_label}: {label}，状态={status}")
                    except Exception as exc:
                        failures += 1
                        progress["failed"] += units
                        self.update_task(parent_id, parallel_done=progress["done"], parallel_failed=progress["failed"])
                        self.append_log(parent_id, f"[PARALLEL-ERROR] 子任务异常: {type(exc).__name__}: {exc}")
                        self.append_log(parent_id, traceback.format_exc())

        parent = self.get_task(parent_id) or {}
        self._apply_parallel_output_progress(parent_id, progress, progress_total, progress_label, progress_state, force_log=True)
        children = parent.get("children") or []
        child_statuses = [(self.get_task(cid) or {}).get("status") for cid in children]

        if parent_id not in self.cancel_flags and failures == 0 and progress_state.get("require_outputs"):
            if progress_state.get("enabled"):
                output_done = min(progress_total, len(progress_state.get("seen_ids") or set()))
                if output_done < progress_total:
                    missing = progress_total - output_done
                    failures += missing
                    progress["done"] = output_done
                    progress["failed"] = max(progress["failed"], missing)
                    self.update_task(parent_id, parallel_done=progress["done"], parallel_failed=progress["failed"])
                    self.append_log(
                        parent_id,
                        (
                            f"[OUTPUT-ERROR] 输出校验失败：预期生成 {progress_total} 个{progress_label}结果，"
                            f"实际只识别到 {output_done} 个。EXE 可能返回了 0 但没有写出结果。"
                        ),
                    )
            else:
                failures += 1
                progress["failed"] = max(progress["failed"], 1)
                self.update_task(parent_id, parallel_failed=progress["failed"])
                self.append_log(parent_id, "[OUTPUT-ERROR] 输出校验已启用，但没有找到可扫描的输出目录或输入标识。")

        if (
            parent_id not in self.cancel_flags
            and failures == 0
            and progress["done"] == progress_total
            and all(s == "success" for s in child_statuses)
        ):
            if not self._run_parallel_tile_merge(parent_id):
                failures += 1
                progress["failed"] = failures
                self.update_task(parent_id, parallel_failed=failures)

        if parent_id in self.cancel_flags or any(s == "cancelled" for s in child_statuses):
            final_status = "cancelled"
            return_code = -1
        elif failures > 0 or progress["done"] < progress_total or any(s != "success" for s in child_statuses):
            final_status = "failed"
            return_code = 1
        else:
            final_status = "success"
            return_code = 0

        self.update_task(
            parent_id,
            status=final_status,
            return_code=return_code,
            ended_at=now_iso(),
            parallel_done=progress["done"],
            parallel_failed=max(progress["failed"], sum(1 for s in child_statuses if s != "success")),
        )

        self.append_log(parent_id, f"[PARALLEL] 并行任务结束，状态={final_status}")
        self._cleanup_runtime_roots_for_task(parent_id, reason=f"并行任务结束，状态={final_status}")
        with self.lock:
            self.learned_child_start_intervals.pop(parent_id, None)
            self.child_start_gate_locks.pop(parent_id, None)
        self.cancel_flags.discard(parent_id)

    def cancel_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False

        # queued 状态尚未启动进程，直接从调度队列移除并标记取消。
        if task.get("status") == "queued":
            self.cancel_flags.add(task_id)
            with self.lock:
                self._remove_from_scheduler_queue_locked(task_id)
                queued_task = self.tasks.get(task_id)
                if queued_task:
                    queued_task["status"] = "cancelled"
                    queued_task["ended_at"] = now_iso()
                    queued_task.setdefault("logs", []).append("[SYSTEM] 排队任务已取消")

                if queued_task and queued_task.get("kind") in {"parallel", "batch_parent"}:
                    for child_id in queued_task.get("children") or []:
                        child = self.tasks.get(child_id)
                        if child and child.get("status") not in TERMINAL_STATUSES:
                            child["status"] = "cancelled"
                            child["ended_at"] = now_iso()
                            child.setdefault("logs", []).append("[SYSTEM] 父任务排队取消，子任务取消")
            self._save_tasks()
            self._cleanup_runtime_roots_for_task(task_id, reason="排队任务取消")
            self._drain_scheduler_queue()
            return True

        # 子任务还没启动时，也允许取消。
        if task.get("status") == "queued" or (task.get("parent_id") and task.get("status") not in TERMINAL_STATUSES and task_id not in self.processes):
            with self.lock:
                child = self.tasks.get(task_id)
                if child:
                    child["status"] = "cancelled"
                    child["ended_at"] = now_iso()
                    child.setdefault("logs", []).append("[SYSTEM] 子任务排队已取消")
            self._save_tasks()
            return True

        # 并行父任务：标记取消，并尽量停止已经启动的所有子进程。
        if task.get("kind") in {"parallel", "batch_parent"}:
            self.cancel_flags.add(task_id)
            any_stopped = False
            for child_id in task.get("children") or []:
                process = self.processes.get(child_id)
                if process is not None:
                    try:
                        if process.poll() is None:
                            process.terminate()
                        any_stopped = True
                    except Exception:
                        pass
                with self.lock:
                    child = self.tasks.get(child_id)
                    if child and child.get("status") not in TERMINAL_STATUSES:
                        child["status"] = "cancelled"
                        child["ended_at"] = now_iso()
                        child.setdefault("logs", []).append("[SYSTEM] 父并行任务已取消，子任务终止")
            with self.lock:
                parent = self.tasks.get(task_id)
                if parent:
                    parent["status"] = "cancelled"
                    parent["ended_at"] = now_iso()
                    parent.setdefault("logs", []).append("[SYSTEM] 并行任务已被手动终止")
            self._save_tasks()
            self._cleanup_runtime_roots_for_task(task_id, reason="并行任务取消")
            return True or any_stopped

        process = self.processes.get(task_id)
        if process is None:
            return False

        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            return False

        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                task["status"] = "cancelled"
                task["ended_at"] = now_iso()
                task.setdefault("logs", []).append("[SYSTEM] 任务已被手动终止")

        self.processes.pop(task_id, None)
        self._save_tasks()
        return True

    def delete_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if task is None:
            return False

        # 删除并行父任务时，同步删除子任务。
        ids_to_delete = [task_id]
        if task.get("kind") in {"parallel", "batch_parent"}:
            ids_to_delete.extend(task.get("children") or [])

        with self.lock:
            for tid in ids_to_delete:
                self._remove_from_scheduler_queue_locked(tid)

        for tid in ids_to_delete:
            process = self.processes.get(tid)
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                except Exception:
                    pass
                self.processes.pop(tid, None)

        cleanup_roots = []
        for tid in ids_to_delete:
            t = self.get_task(tid) or {}
            cleanup_roots.extend(t.get("cleanup_roots") or [])
            cleanup_roots.extend((t.get("inputs") or {}).get("_parallel_cleanup_roots") or [])

        if cleanup_roots:
            self._cleanup_runtime_roots(task_id, cleanup_roots, reason="任务记录删除")

        with self.lock:
            for tid in ids_to_delete:
                self.tasks.pop(tid, None)

        self._save_tasks()
        return True
