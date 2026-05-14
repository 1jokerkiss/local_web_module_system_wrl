import json
import os
import re
import subprocess
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        # 这里按“内存较重的遥感模块”做保守默认值：
        # - 建议值约为 CPU 核数的 1/3，最高 8；24 核时建议 8。
        # - 上限值约为 CPU 核数的 1/2，最高 12；24 核时上限 12。
        # 如需手动覆盖，可设置环境变量：
        # LOCAL_WEB_SUGGESTED_PROCESS_SLOTS / LOCAL_WEB_MAX_PROCESS_SLOTS。
        # queued 状态的任务会留在队列里；有空闲槽位后自动启动。
        self.cpu_count = max(1, int(os.cpu_count() or 1))
        default_suggested_slots = max(1, min(8, (self.cpu_count + 2) // 3))
        default_max_slots = max(default_suggested_slots, min(12, max(1, self.cpu_count // 2)))

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
        self.cpu_busy_threshold = float(os.environ.get("LOCAL_WEB_CPU_QUEUE_THRESHOLD", "85"))
        self.scheduler_queue: list[Dict[str, Any]] = []
        self.active_slots: Dict[str, int] = {}
        self.drain_lock = threading.Lock()

        self._load_tasks()
        self._mark_interrupted_tasks()

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
        """读取本机 CPU 使用率；优先用 psutil，没有安装时用 load average 粗略估算。"""
        try:
            import psutil  # type: ignore
            return float(psutil.cpu_percent(interval=0.0))
        except Exception:
            pass

        try:
            if hasattr(os, "getloadavg"):
                load1, _, _ = os.getloadavg()
                return max(0.0, min(100.0, float(load1) / max(1, self.cpu_count) * 100.0))
        except Exception:
            pass
        return None

    def _running_process_cpu_percent(self) -> float | None:
        """读取当前由平台启动的模块进程 CPU 占用总和；没有 psutil 时返回 None。"""
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
                    total += float(psutil.Process(process.pid).cpu_percent(interval=0.0))
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
            task["queue_reason"] = (
                f"等待本地 CPU 空闲：当前占用 {used}/{self.max_process_slots}，"
                f"本任务需要 {requested} 个进程槽"
            )
            pos += 1

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
        return {
            "cpu_count": self.cpu_count,
            "suggested_workers": self.suggested_process_slots,
            "max_workers": self.max_process_slots,
            "running_workers": running_workers,
            "available_workers": max(0, self.max_process_slots - running_workers),
            "active_task_count": active_task_count,
            "queued_task_count": queued_task_count,
            "cpu_percent": cpu_percent,
            "running_process_cpu_percent": process_cpu_percent,
            "cpu_busy_threshold": self.cpu_busy_threshold,
            "active_tasks": active_tasks,
        }

    def _can_start_queued_item_locked(self, item: Dict[str, Any]) -> tuple[bool, str]:
        requested = self._normalize_requested_slots(item.get("requested_slots"))
        used = self._used_slots_locked()
        if used + requested > self.max_process_slots:
            return False, f"进程数超过本机 CPU 上限：当前 {used}/{self.max_process_slots}，本任务需要 {requested}"

        cpu_percent = self._system_cpu_percent()
        if used > 0 and cpu_percent is not None and cpu_percent >= self.cpu_busy_threshold:
            return False, f"当前 CPU 使用率 {cpu_percent:.1f}% 已超过阈值 {self.cpu_busy_threshold:.0f}%"

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

        requested_slots = inputs.get("parallel_workers") or inputs.get("_parallel_workers") or 1
        self._enqueue_task_runner(
            task["id"],
            self._run_process_task,
            (task["id"], command, working_dir, env),
            requested_slots=requested_slots,
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

        self._enqueue_task_runner(
            parent["id"],
            self._run_parallel_task,
            (parent["id"], jobs, max_workers),
            requested_slots=max_workers,
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

        self._enqueue_task_runner(
            parent["id"],
            self._run_batch_group,
            (parent["id"], child_job_map, max_parallel),
            requested_slots=max_parallel,
        )
        return self.get_task(parent["id"]) or parent

    def _run_batch_group(
        self,
        parent_id: str,
        child_job_map: Dict[str, Dict[str, Any]],
        max_parallel: int,
    ):
        total = len(child_job_map)
        self.update_task(
            parent_id,
            status="running",
            started_at=now_iso(),
            parallel_total=total,
            parallel_done=0,
            parallel_failed=0,
            parallel_started=0,
            parallel_running=0,
        )
        self.append_log(parent_id, f"[INFO] 批处理开始，共 {total} 个子任务")
        self.append_log(parent_id, f"[INFO] 最大并发数 = {max_parallel}")

        progress_lock = threading.Lock()
        progress = {"done": 0, "failed": 0, "started": 0, "running": 0}

        def _worker(child_id: str, job: Dict[str, Any]):
            child_snapshot = self.get_task(child_id) or {}
            if parent_id in self.cancel_flags or child_snapshot.get("status") == "cancelled":
                self.update_task(child_id, status="cancelled", ended_at=now_iso())
                return child_id

            with progress_lock:
                progress["started"] += 1
                progress["running"] += 1
                self.update_task(
                    parent_id,
                    parallel_started=progress["started"],
                    parallel_running=progress["running"],
                )
                self.append_log(
                    parent_id,
                    f"[INFO] 当前已启动 {progress['started']}/{total} 个子任务，正在运行 {progress['running']} 个，子任务ID={child_id}",
                )

            try:
                self._run_process_task(
                    child_id,
                    job["command"],
                    job.get("working_dir"),
                    job.get("env"),
                )
            finally:
                with progress_lock:
                    progress["running"] = max(0, progress["running"] - 1)
                    self.update_task(parent_id, parallel_running=progress["running"])
            return child_id

        failures = 0
        with ThreadPoolExecutor(max_workers=max(1, int(max_parallel or 1))) as executor:
            futures = {
                executor.submit(_worker, child_id, job): child_id
                for child_id, job in child_job_map.items()
            }
            for future in as_completed(futures):
                child_id = futures[future]
                try:
                    future.result()
                    task = self.get_task(child_id) or {}
                    status = task.get("status")
                    return_code = task.get("return_code")
                    if status != "success":
                        failures += 1
                    self.append_log(
                        parent_id,
                        f"[INFO] 子任务完成: {child_id}, 状态={status}, return_code={return_code}",
                    )
                except Exception as e:
                    failures += 1
                    self.append_log(parent_id, f"[ERROR] 子任务异常: {child_id} -> {repr(e)}")

                with progress_lock:
                    progress["done"] += 1
                    progress["failed"] = failures
                    self.update_task(
                        parent_id,
                        parallel_done=progress["done"],
                        parallel_failed=progress["failed"],
                    )

        if parent_id in self.cancel_flags:
            final_status = "cancelled"
            return_code = -1
        else:
            final_status = "success" if failures == 0 else "failed"
            return_code = 0 if failures == 0 else 1

        self.update_task(
            parent_id,
            status=final_status,
            ended_at=now_iso(),
            return_code=return_code,
            parallel_done=total,
            parallel_failed=failures,
        )
        self.append_log(parent_id, f"[INFO] 批处理结束，失败数={failures}")
        self.cancel_flags.discard(parent_id)

    def _stream_reader(self, pipe, task_id: str, prefix: str):
        try:
            if pipe is None:
                return

            buffer = bytearray()
            last_line = {}

            while True:
                chunk = pipe.read(256)

                if not chunk:
                    break

                for b in chunk:
                    # tqdm 使用 \r 原地刷新，普通 print 使用 \n 换行；
                    # 两种都应该立刻刷到前端。
                    if b in (10, 13):  # \n or \r
                        if buffer:
                            self._append_stream_bytes(
                                task_id,
                                prefix,
                                bytes(buffer),
                                last_line,
                            )
                            buffer.clear()
                    else:
                        buffer.append(b)

            if buffer:
                self._append_stream_bytes(
                    task_id,
                    prefix,
                    bytes(buffer),
                    last_line,
                )

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

    @staticmethod
    def clean_process_line(text: str) -> str:
        """
        清理子进程输出：
        1. 去掉 ANSI 控制符；
        2. 去掉 Unicode replacement character，避免出现 ��；
        3. 去掉其它不可见控制符，但保留普通文本。
        """
        text = str(text or "")
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        text = text.replace("\ufffd", "").replace("�", "")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        return text.strip()

    def _append_stream_bytes(self, task_id: str, prefix: str, raw: bytes, last_line: dict):
        if not raw:
            return

        line = self.clean_process_line(self.decode_process_output(raw))

        if not line:
            return

        # tqdm 会频繁刷新同一行，重复内容不反复写入，避免 tasks.json 暴涨。
        key = f"{prefix}_last"
        if last_line.get(key) == line:
            return

        last_line[key] = line
        self.append_log(task_id, f"[{prefix}] {line}")
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
        self.update_task(
            parent_id,
            status="running",
            started_at=now_iso(),
            parallel_total=total,
            parallel_done=0,
            parallel_failed=0,
            parallel_started=0,
            parallel_running=0,
        )
        self.append_log(parent_id, f"[PARALLEL] 并行任务启动，总任务数={total}，并行数={max_workers}")

        index_lock = threading.Lock()
        progress_lock = threading.Lock()
        next_index = {"value": 0}
        progress = {"done": 0, "failed": 0, "started": 0, "running": 0}

        def next_job() -> tuple[int, Dict[str, Any]] | None:
            with index_lock:
                if parent_id in self.cancel_flags:
                    return None
                idx = next_index["value"]
                if idx >= total:
                    return None
                next_index["value"] += 1
                return idx, jobs[idx]

        def worker(worker_no: int):
            while True:
                item = next_job()
                if item is None:
                    return
                idx, spec = item
                label = spec.get("label") or f"子任务 {idx + 1}"
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
                        "worker_no": worker_no,
                        "job_index": idx + 1,
                        "owner_username": owner_username,
                    },
                )
                with self.lock:
                    parent = self.tasks.get(parent_id)
                    if parent:
                        parent.setdefault("children", []).append(child["id"])
                self._save_tasks()

                with progress_lock:
                    progress["started"] += 1
                    progress["running"] += 1
                    self.update_task(
                        parent_id,
                        parallel_started=progress["started"],
                        parallel_running=progress["running"],
                    )
                    self.append_log(
                        parent_id,
                        f"[PARALLEL] 当前已启动 {progress['started']}/{total} 个任务，正在运行 {progress['running']} 个；Worker-{worker_no} 启动 {idx + 1}/{total}: {label}，子任务ID={child['id']}",
                    )

                try:
                    self._run_process_task(
                        child["id"],
                        spec.get("command") or [],
                        spec.get("working_dir"),
                        spec.get("env"),
                    )
                finally:
                    child_task = self.get_task(child["id"]) or {}
                    child_status = child_task.get("status")
                    with progress_lock:
                        progress["done"] += 1
                        progress["running"] = max(0, progress["running"] - 1)
                        if child_status != "success":
                            progress["failed"] += 1
                        self.update_task(
                            parent_id,
                            parallel_done=progress["done"],
                            parallel_failed=progress["failed"],
                            parallel_running=progress["running"],
                        )
                    self.append_log(
                        parent_id,
                        f"[PARALLEL] 完成 {progress['done']}/{total}: {label}，状态={child_status}，当前仍在运行 {progress['running']} 个",
                    )

        thread_count = max(1, min(max_workers, total))
        workers = [
            threading.Thread(target=worker, args=(i + 1,), daemon=True)
            for i in range(thread_count)
        ]
        for t in workers:
            t.start()
        for t in workers:
            t.join()

        parent = self.get_task(parent_id) or {}
        children = parent.get("children") or []
        child_statuses = [(self.get_task(cid) or {}).get("status") for cid in children]
        if parent_id in self.cancel_flags or any(s == "cancelled" for s in child_statuses):
            status = "cancelled"
            return_code = -1
        elif any(s != "success" for s in child_statuses):
            status = "failed"
            return_code = 1
        else:
            status = "success"
            return_code = 0

        self.update_task(parent_id, status=status, return_code=return_code, ended_at=now_iso())
        self.append_log(parent_id, f"[PARALLEL] 并行任务结束，状态={status}")
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
