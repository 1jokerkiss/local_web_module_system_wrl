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

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

TERMINAL_STATUSES = {"success", "failed", "cancelled"}

class TaskManager:
    def __init__(self, tasks_file: str | Path):
        self.tasks_file = Path(tasks_file)
        self.tasks_file.parent.mkdir(parents=True, exist_ok=True)

        self.lock = threading.RLock()
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.processes: Dict[str, subprocess.Popen] = {}
        self.cancel_flags: set[str] = set()

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
        self.suggested_process_slots = max(1, min(self.max_process_slots, env_suggested_slots))
        # 顶层排队只做“临界保护”。一般负载不阻止父任务启动，避免一直 queued。
        self.cpu_busy_threshold = float(os.environ.get("LOCAL_WEB_CPU_QUEUE_THRESHOLD", "99"))
        self.scheduler_queue: list[Dict[str, Any]] = []
        self.active_slots: Dict[str, int] = {}
        self.drain_lock = threading.Lock()
        # 运行中保护：批处理/并行任务启动子进程前会检查 CPU、内存和磁盘压力，压力过高时暂停启动新子任务。
        # 子进程启动保护：逐个启动子任务；达到阈值时暂停启动新的子任务。
        self.child_launch_cpu_threshold = float(os.environ.get("LOCAL_WEB_CHILD_START_CPU_THRESHOLD", "96"))
        self.child_launch_memory_threshold = float(os.environ.get("LOCAL_WEB_CHILD_START_MEMORY_THRESHOLD", "99"))
        self.child_launch_min_memory_gb = float(os.environ.get("LOCAL_WEB_CHILD_START_MIN_MEMORY_GB", "0.3"))
        self.child_launch_disk_threshold = float(os.environ.get("LOCAL_WEB_CHILD_START_DISK_THRESHOLD", "99.5"))
        self.child_launch_min_disk_free_gb = float(os.environ.get("LOCAL_WEB_CHILD_START_MIN_DISK_FREE_GB", "0.5"))
        self.child_launch_wait_seconds = float(os.environ.get("LOCAL_WEB_CHILD_START_WAIT_SECONDS", "2"))
        self.child_start_stagger_seconds = float(os.environ.get("LOCAL_WEB_CHILD_START_STAGGER_SECONDS", "0.5"))
        self._last_pressure_log_at: Dict[str, float] = {}

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
        """服务重启后，内存里的进程和调度队列都不存在了。把旧的 running/queued 标记为 cancelled。"""
        changed = False
        with self.lock:
            for task in self.tasks.values():
                if task.get("status") in {"queued", "running"}:
                    task["status"] = "cancelled"
                    task["ended_at"] = now_iso()
                    task.setdefault("logs", []).append("[SYSTEM] 服务已重启，历史未完成任务已自动取消")
                    changed = True
        if changed:
            self._save_tasks()

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

    def _runtime_pressure_reason(self) -> str:
        """返回是否应该暂停启动新的子进程。

        轻量化策略：
        - 顶层任务不因为内存 80% 多就长期 queued；
        - 真正启动每一个子进程前，才检查 CPU/内存/磁盘和当前平台进程数；
        - 已启动的进程不强杀，只暂停后续启动，等负载下降再继续。
        """
        active_processes = self._active_process_count()
        if active_processes >= self.max_process_slots:
            return f"平台已启动 {active_processes}/{self.max_process_slots} 个模块进程，等待已有进程完成"

        cpu = self._system_cpu_percent()
        if cpu is not None and cpu >= self.child_launch_cpu_threshold:
            return f"CPU 使用率 {cpu:.1f}% 已超过暂停启动阈值 {self.child_launch_cpu_threshold:.0f}%"

        mem = self._virtual_memory_snapshot()
        mem_percent = mem.get("percent")
        mem_available = mem.get("available_gb")
        if mem_percent is not None and mem_percent >= self.child_launch_memory_threshold:
            return f"内存使用率 {mem_percent:.1f}% 已超过暂停启动阈值 {self.child_launch_memory_threshold:.0f}%"
        if mem_available is not None and mem_available <= self.child_launch_min_memory_gb:
            return f"可用内存仅 {mem_available:.1f}GB，低于最低阈值 {self.child_launch_min_memory_gb:.1f}GB"

        disk = self._disk_usage_snapshot()
        disk_percent = disk.get("percent")
        disk_free = disk.get("free_gb")
        if disk_percent is not None and disk_percent >= self.child_launch_disk_threshold:
            return f"磁盘使用率 {disk_percent:.1f}% 已超过暂停启动阈值 {self.child_launch_disk_threshold:.0f}%"
        if disk_free is not None and disk_free <= self.child_launch_min_disk_free_gb:
            return f"磁盘剩余空间仅 {disk_free:.1f}GB，低于最低阈值 {self.child_launch_min_disk_free_gb:.1f}GB"

        return ""

    def _wait_until_safe_to_start_child(self, parent_id: str, label: str):
        """父并行任务运行期间，系统压力过高时不再启动新子进程，等压力下降再继续。

        需要连续两次采样都安全才放行，避免刚启动几个进程时 CPU 还没来得及升高，
        后续进程又被瞬间全部拉起导致电脑卡死。
        """
        safe_samples = 0
        while parent_id not in self.cancel_flags:
            reason = self._runtime_pressure_reason()
            if not reason:
                safe_samples += 1
                if safe_samples >= 2:
                    return
                time.sleep(max(1.0, min(self.child_launch_wait_seconds, 3.0)))
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
            time.sleep(max(1.0, self.child_launch_wait_seconds))

    def get_system_resource_info(self) -> Dict[str, Any]:
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
        with self.lock:
            data = list(self.tasks.values())
            self.tasks_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

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
        self._save_tasks()


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
        max_workers = max(1, int(max_workers or 1))
        parent = self.create_task(
            module_id=module_id,
            module_name=module_name,
            command=[],
            inputs=inputs,
            kind="parallel",
            extra={
                "parallel_total": len(jobs),
                "parallel_done": 0,
                "parallel_failed": 0,
                "max_workers": max_workers,
                "owner_username": str(owner_username or ""),
            },
        )

        self._append_parallel_adjustment_log(parent["id"], inputs)
        self._enqueue_task_runner(
            parent["id"],
            self._run_parallel_task,
            (parent["id"], jobs, max_workers),
            requested_slots=1,
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
        """Submit a batch parent task using the old tested ThreadPoolExecutor process-pool style.

        Each job becomes one child task. max_parallel controls how many child jobs are
        executed at the same time. Closing a task window in the UI does not stop the
        background job; cancel_task is still available to terminate running processes.
        """
        max_parallel = max(1, int(max_parallel or 1))
        parent = self.create_task(
            module_id=module_id,
            module_name=f"{module_name} 批处理",
            command=[],
            inputs={"job_count": len(jobs), "parallel_workers": max_parallel},
            kind="batch_parent",
            extra={
                "parallel_total": len(jobs),
                "parallel_done": 0,
                "parallel_failed": 0,
                "max_workers": max_parallel,
                "owner_username": str(owner_username or ""),
            },
        )

        child_ids: list[str] = []
        child_job_map: dict[str, Dict[str, Any]] = {}

        for idx, job in enumerate(jobs, start=1):
            child = self.create_task(
                module_id=module_id,
                module_name=f"{module_name} [{idx}/{len(jobs)}]",
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
        first_job_inputs = next(iter(child_job_map.values()), {}).get("inputs") if child_job_map else None
        self._append_parallel_adjustment_log(parent["id"], first_job_inputs)

        self._enqueue_task_runner(
            parent["id"],
            self._run_batch_group,
            (parent["id"], child_job_map, max_parallel),
            requested_slots=1,
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
        self.append_log(parent_id, f"[INFO] 批处理开始，共 {total} 个子任务")
        self.append_log(parent_id, f"[INFO] 用户选择并发数 = {max_parallel}；系统会逐个启动子进程，负载高时暂停启动新进程")
        self.append_log(parent_id, "[SAFE] 轻量化调度：不一次性提交全部子任务，只在 CPU/内存/磁盘安全时启动下一个子任务。")

        job_items = list(child_job_map.items())
        next_index = 0
        failures = 0
        done = 0

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
            while (next_index < total or running) and parent_id not in self.cancel_flags:
                launched_any = False
                while next_index < total and len(running) < max_parallel and parent_id not in self.cancel_flags:
                    child_id, job = job_items[next_index]
                    label = job.get("label") or child_id
                    reason = self._runtime_pressure_reason()
                    if reason:
                        now = time.time()
                        last = self._last_pressure_log_at.get(parent_id, 0.0)
                        if now - last >= 8:
                            self._last_pressure_log_at[parent_id] = now
                            self.append_log(
                                parent_id,
                                f"[SAFE] 暂停启动新子任务 {label}：{reason}。当前运行 {len(running)} 个；已完成 {done}/{total}。",
                            )
                        break

                    self.append_log(parent_id, f"[INFO] 启动子任务 {next_index + 1}/{total}: {label}；当前运行 {len(running) + 1}/{max_parallel}")
                    future = executor.submit(_worker, child_id, job)
                    running[future] = child_id
                    next_index += 1
                    launched_any = True
                    time.sleep(max(0.0, self.child_start_stagger_seconds))

                if not running:
                    time.sleep(max(1.0, self.child_launch_wait_seconds))
                    continue

                done_set, _ = wait(set(running.keys()), timeout=1.0, return_when=FIRST_COMPLETED)
                if not done_set and not launched_any:
                    time.sleep(0.5)
                    continue

                for future in done_set:
                    child_id = running.pop(future, "")
                    try:
                        future.result()
                        task = self.get_task(child_id) or {}
                        status = task.get("status")
                        return_code = task.get("return_code")
                        if status != "success":
                            failures += 1
                        self.append_log(parent_id, f"[INFO] 子任务完成: {child_id}, 状态={status}, return_code={return_code}")
                    except Exception as e:
                        failures += 1
                        self.append_log(parent_id, f"[ERROR] 子任务异常: {child_id} -> {repr(e)}")
                        self.append_log(parent_id, traceback.format_exc())

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

        self._log_runtime_context(task_id, command, working_dir, merged_env)

        try:
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

            with self.lock:
                self.processes[task_id] = process
                task = self.tasks.get(task_id)
                if task:
                    task["status"] = "running"
                    task["pid"] = process.pid
                    task["started_at"] = now_iso()
                    task.setdefault("logs", []).append(
                        f"[INFO] 进程已启动，PID = {process.pid}"
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
                    if task.get("status") != "cancelled":
                        task["return_code"] = return_code
                        task["status"] = "success" if return_code == 0 else "failed"
                        task["ended_at"] = now_iso()
                    task.setdefault("logs", []).append(
                        f"[INFO] 进程结束，return_code = {return_code}"
                    )

                    hint = self._hint_from_return_code(return_code)
                    if hint:
                        task.setdefault("logs", []).append(f"[HINT] {hint}")

                    if return_code != 0 and not task.get("logs"):
                        task.setdefault("logs", []).append(
                            "[WARN] 进程失败，但没有捕获到 stdout/stderr。"
                        )

            self.processes.pop(task_id, None)
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

            self.processes.pop(task_id, None)
            self._save_tasks()

    def _run_parallel_task(self, parent_id: str, jobs: List[Dict[str, Any]], max_workers: int):
        total = len(jobs)
        max_workers = max(1, min(int(max_workers or 1), max(1, total)))

        self.update_task(
            parent_id,
            status="running",
            started_at=now_iso(),
            parallel_total=total,
            parallel_done=0,
            parallel_failed=0,
            max_workers=max_workers,
        )

        self.append_log(parent_id, f"[PARALLEL] 并行任务启动：总任务数={total}，用户选择并行数={max_workers}")
        self.append_log(parent_id, "[SAFE] 轻量化调度：不再按模型文件大小直接降为 1；逐个启动子进程，每次启动前检查 CPU/内存/磁盘，压力高时暂停启动后续子任务。")

        progress = {"done": 0, "failed": 0}

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

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            running: Dict[Any, int] = {}
            while (next_index < total or running) and parent_id not in self.cancel_flags:
                launched_any = False
                while next_index < total and len(running) < max_workers and parent_id not in self.cancel_flags:
                    spec = jobs[next_index]
                    label = spec.get("label") or f"子任务 {next_index + 1}"
                    reason = self._runtime_pressure_reason()
                    if reason:
                        now = time.time()
                        last = self._last_pressure_log_at.get(parent_id, 0.0)
                        if now - last >= 8:
                            self._last_pressure_log_at[parent_id] = now
                            self.append_log(
                                parent_id,
                                f"[SAFE] 暂停启动新子任务 {label}：{reason}。当前运行 {len(running)} 个；已完成 {progress['done']}/{total}。",
                            )
                        break

                    future = executor.submit(run_one, next_index, spec)
                    running[future] = next_index
                    self.append_log(parent_id, f"[PARALLEL] 已提交 {next_index + 1}/{total}；当前运行 {len(running)}/{max_workers}")
                    next_index += 1
                    launched_any = True
                    time.sleep(max(0.0, self.child_start_stagger_seconds))

                if not running:
                    time.sleep(max(1.0, self.child_launch_wait_seconds))
                    continue

                done_set, _ = wait(set(running.keys()), timeout=1.0, return_when=FIRST_COMPLETED)
                if not done_set and not launched_any:
                    time.sleep(0.5)
                    continue

                for future in done_set:
                    idx = running.pop(future, -1)
                    label = jobs[idx].get("label") if 0 <= idx < len(jobs) else "子任务"
                    try:
                        result = future.result()
                        child_id, status = result if result else (None, "cancelled")
                        if status != "success":
                            failures += 1
                        progress["done"] += 1
                        progress["failed"] = failures
                        self.update_task(parent_id, parallel_done=progress["done"], parallel_failed=progress["failed"])
                        self.append_log(parent_id, f"[PARALLEL] 完成 {progress['done']}/{total}: {label}，状态={status}")
                    except Exception as exc:
                        failures += 1
                        progress["done"] += 1
                        progress["failed"] = failures
                        self.update_task(parent_id, parallel_done=progress["done"], parallel_failed=progress["failed"])
                        self.append_log(parent_id, f"[PARALLEL-ERROR] 子任务异常: {type(exc).__name__}: {exc}")
                        self.append_log(parent_id, traceback.format_exc())

        parent = self.get_task(parent_id) or {}
        children = parent.get("children") or []
        child_statuses = [(self.get_task(cid) or {}).get("status") for cid in children]

        if parent_id in self.cancel_flags or any(s == "cancelled" for s in child_statuses):
            final_status = "cancelled"
            return_code = -1
        elif failures > 0 or progress["done"] < total or any(s != "success" for s in child_statuses):
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
            parallel_failed=sum(1 for s in child_statuses if s != "success"),
        )

        self.append_log(parent_id, f"[PARALLEL] 并行任务结束，状态={final_status}")
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

        with self.lock:
            for tid in ids_to_delete:
                self.tasks.pop(tid, None)

        self._save_tasks()
        return True
