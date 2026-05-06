import json
import os
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

        self.lock = threading.Lock()
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.processes: Dict[str, subprocess.Popen] = {}
        self.cancel_flags: set[str] = set()

        self._load_tasks()

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

    def _save_tasks(self):
        with self.lock:
            data = list(self.tasks.values())
            self.tasks_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self.lock:
            items = list(self.tasks.values())
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
    ) -> Dict[str, Any]:
        task = self.create_task(
            module_id=module_id,
            module_name=module_name,
            command=command,
            inputs=inputs,
            kind="module",
        )

        thread = threading.Thread(
            target=self._run_process_task,
            args=(task["id"], command, working_dir, env),
            daemon=True,
        )
        thread.start()
        return task

    def submit_parallel_module_task(
        self,
        module_id: str,
        module_name: str,
        jobs: List[Dict[str, Any]],
        inputs: Dict[str, Any],
        max_workers: int = 2,
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
            },
        )

        thread = threading.Thread(
            target=self._run_parallel_task,
            args=(parent["id"], jobs, max_workers),
            daemon=True,
        )
        thread.start()
        return parent

    def submit_batch_group(
        self,
        module_id: str,
        module_name: str,
        jobs: List[Dict[str, Any]],
        max_parallel: int,
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
                extra={"parent_id": parent["id"], "job_index": idx},
                auto_save=False,
            )
            child_ids.append(child["id"])
            child_job_map[child["id"]] = job

        with self.lock:
            self.tasks[parent["id"]]["children"] = child_ids
            self.tasks[parent["id"]]["status"] = "queued"
        self._save_tasks()

        thread = threading.Thread(
            target=self._run_batch_group,
            args=(parent["id"], child_job_map, max_parallel),
            daemon=True,
        )
        thread.start()
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
        )
        self.append_log(parent_id, f"[INFO] 批处理开始，共 {total} 个子任务")
        self.append_log(parent_id, f"[INFO] 最大并发数 = {max_parallel}")

        progress_lock = threading.Lock()
        progress = {"done": 0, "failed": 0}

        def _worker(child_id: str, job: Dict[str, Any]):
            if parent_id in self.cancel_flags:
                self.update_task(child_id, status="cancelled", ended_at=now_iso())
                return child_id
            self._run_process_task(
                child_id,
                job["command"],
                job.get("working_dir"),
                job.get("env"),
            )
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

            for raw in iter(pipe.readline, ""):
                if not raw:
                    break
                line = raw.rstrip("\r\n")
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

        if len(command) > 1:
            cfg = Path(command[1])
            self.append_log(task_id, f"[INFO] config/input = {cfg}")
            if cfg.exists() and cfg.suffix.lower() == ".json":
                try:
                    content = cfg.read_text(encoding="utf-8")
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
                text=True,
                encoding="utf-8",
                errors="replace",
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
        )
        self.append_log(parent_id, f"[PARALLEL] 并行任务启动，总任务数={total}，并行数={max_workers}")

        index_lock = threading.Lock()
        progress_lock = threading.Lock()
        next_index = {"value": 0}
        progress = {"done": 0, "failed": 0}

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
                child = self.create_task(
                    module_id=spec.get("module_id", ""),
                    module_name=spec.get("module_name", label),
                    command=spec.get("command") or [],
                    inputs=spec.get("inputs") or {},
                    kind="module",
                    extra={"parent_id": parent_id, "worker_no": worker_no, "job_index": idx + 1},
                )
                with self.lock:
                    parent = self.tasks.get(parent_id)
                    if parent:
                        parent.setdefault("children", []).append(child["id"])
                self._save_tasks()

                self.append_log(parent_id, f"[PARALLEL] Worker-{worker_no} 启动 {idx + 1}/{total}: {label}")
                self._run_process_task(
                    child["id"],
                    spec.get("command") or [],
                    spec.get("working_dir"),
                    spec.get("env"),
                )
                child_task = self.get_task(child["id"]) or {}
                child_status = child_task.get("status")
                with progress_lock:
                    progress["done"] += 1
                    if child_status != "success":
                        progress["failed"] += 1
                    self.update_task(
                        parent_id,
                        parallel_done=progress["done"],
                        parallel_failed=progress["failed"],
                    )
                self.append_log(
                    parent_id,
                    f"[PARALLEL] 完成 {progress['done']}/{total}: {label}，状态={child_status}",
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
