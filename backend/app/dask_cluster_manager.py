from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


DASK_PACKAGE_SPEC = "dask[distributed]==2024.7.1"
BOKEH_PACKAGE_SPEC = "bokeh>=3.1,<4"


class DaskClusterError(RuntimeError):
    pass


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _human_gb(value: Any) -> float | None:
    try:
        return round(float(value) / (1024 ** 3), 2)
    except Exception:
        return None


def _worker_shared_path_probe(path_text: str) -> dict:
    """在 Dask Worker 上检测共享目录是否可访问。"""
    import os
    import socket
    import tempfile
    from pathlib import Path

    path = Path(path_text)
    result = {
        "hostname": socket.gethostname(),
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "writable": False,
        "error": "",
    }
    if path.exists() and path.is_dir():
        probe = None
        try:
            fd, probe = tempfile.mkstemp(prefix=".dask_write_test_", dir=str(path))
            os.close(fd)
            os.unlink(probe)
            result["writable"] = True
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            if probe:
                try:
                    os.unlink(probe)
                except Exception:
                    pass
    return result


class DaskClusterManager:
    """
    管理本机 Dask 安装、Scheduler、Worker、集群状态和任务执行模式。

    设计约束：
    1. 每台电脑都运行本系统后端；
    2. 主节点在“分布式”页面创建集群；
    3. 子节点在本机页面输入主节点 IP 和加入令牌；
    4. Dask 只负责调度，算法仍以 subprocess 方式运行；
    5. 大型遥感数据不通过 Dask 序列化传输，分布式模式要求所有节点能访问相同路径。
    """

    def __init__(self, backend_dir: str | Path, project_root: str | Path | None = None):
        self.backend_dir = Path(backend_dir).resolve()
        self.project_root = Path(project_root).resolve() if project_root else self.backend_dir.parent
        self.data_dir = self.backend_dir / "data"
        self.runtime_dir = self.backend_dir / "runtime" / "dask_cluster"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.data_dir / "dask_cluster.json"
        self.lock = threading.RLock()
        self._client = None

        self.scheduler_log = self.runtime_dir / "scheduler.log"
        self.worker_log = self.runtime_dir / "worker.log"
        self.install_log = self.runtime_dir / "install.log"

        self.state = self._load_state()

    def _default_state(self) -> dict:
        return {
            "role": "standalone",
            "execution_mode": "local",
            "cluster_id": "",
            "join_token": "",
            "scheduler_address": "",
            "head_api_url": "",
            "scheduler_port": 8786,
            "dashboard_port": 8787,
            "api_port": 8000,
            "scheduler_pid": None,
            "worker_pid": None,
            "worker_name": socket.gethostname(),
            "nworkers": 1,
            "nthreads": 1,
            "memory_limit": "auto",
            "shared_runtime_root": "",
            "package_spec": DASK_PACKAGE_SPEC,
            "created_at": "",
            "joined_at": "",
            "last_error": "",
        }

    def _load_state(self) -> dict:
        default = self._default_state()
        if not self.state_file.exists():
            return default
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                default.update(raw)
        except Exception:
            pass
        return default

    def _save_state(self):
        with self.lock:
            self.state_file.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    @staticmethod
    def local_ip() -> str:
        candidates: list[str] = []
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                candidates.append(str(s.getsockname()[0]))
        except Exception:
            pass
        try:
            _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
            candidates.extend(addresses)
        except Exception:
            pass

        for ip in candidates:
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
        return "127.0.0.1"

    @staticmethod
    def _package_version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except Exception:
            return ""

    def package_info(self) -> dict:
        return {
            "installed": bool(self._package_version("distributed")),
            "dask_version": self._package_version("dask"),
            "distributed_version": self._package_version("distributed"),
            "package_spec": str(self.state.get("package_spec") or DASK_PACKAGE_SPEC),
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
        }

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        try:
            pid_int = int(pid)
        except Exception:
            return False
        if pid_int <= 0:
            return False

        try:
            import psutil  # type: ignore
            return bool(psutil.pid_exists(pid_int) and psutil.Process(pid_int).is_running())
        except Exception:
            pass

        try:
            os.kill(pid_int, 0)
            return True
        except Exception:
            return False

    @staticmethod
    def _terminate_pid(pid: Any):
        try:
            pid_int = int(pid)
        except Exception:
            return
        if pid_int <= 0:
            return

        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid_int), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
            )
            return

        try:
            import signal
            os.kill(pid_int, signal.SIGTERM)
        except Exception:
            pass

    def _close_client(self):
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close(timeout=2)
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass

    def install(self, package_spec: str = "", upgrade: bool = False) -> dict:
        spec = (package_spec or self.state.get("package_spec") or DASK_PACKAGE_SPEC).strip()
        # Python 3.9 从 Dask 2024.8.1 起不再受支持，因此默认固定到 2024.7.1。
        if sys.version_info < (3, 10) and "==" not in spec:
            spec = DASK_PACKAGE_SPEC

        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
        ]
        if upgrade:
            cmd.append("--upgrade")
        cmd.extend([spec, BOKEH_PACKAGE_SPEC, "psutil"])

        started = _now_iso()
        result = subprocess.run(
            cmd,
            cwd=str(self.backend_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            shell=False,
        )
        log_text = (
            f"[{started}] COMMAND: {' '.join(cmd)}\n"
            f"RETURN_CODE: {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}\n"
        )
        self.install_log.write_text(log_text, encoding="utf-8")

        if result.returncode != 0:
            raise DaskClusterError(
                "Dask 安装失败。\n"
                + (result.stderr or result.stdout or "pip 未返回错误详情")[-5000:]
            )

        importlib.invalidate_caches()
        self.state["package_spec"] = spec
        self.state["last_error"] = ""
        self._save_state()
        return {
            "message": "Dask Distributed 安装完成",
            "package": self.package_info(),
            "log": log_text[-8000:],
        }

    def ensure_installed(self, auto_install: bool = False):
        if self.package_info()["installed"]:
            return
        if auto_install:
            self.install()
            return
        raise DaskClusterError("当前 Python 环境未安装 Dask Distributed，请先点击“安装 Dask”。")

    def open_firewall(self, api_port: int = 8000, scheduler_port: int = 8786, dashboard_port: int = 8787) -> dict:
        if os.name != "nt":
            return {"success": True, "message": "非 Windows 系统无需执行 Windows 防火墙命令", "results": []}

        ports = [
            ("LocalWeb-Dask-API", int(api_port)),
            ("LocalWeb-Dask-Scheduler", int(scheduler_port)),
            ("LocalWeb-Dask-Dashboard", int(dashboard_port)),
        ]
        results = []
        all_ok = True
        for rule_name, port in ports:
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule_name}"],
                capture_output=True,
                text=True,
                shell=False,
            )
            result = subprocess.run(
                [
                    "netsh", "advfirewall", "firewall", "add", "rule",
                    f"name={rule_name}", "dir=in", "action=allow",
                    "protocol=TCP", f"localport={port}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            ok = result.returncode == 0
            all_ok = all_ok and ok
            results.append({
                "rule": rule_name,
                "port": port,
                "success": ok,
                "output": (result.stdout or result.stderr or "").strip(),
            })

        return {
            "success": all_ok,
            "message": "防火墙规则已配置" if all_ok else "部分防火墙规则配置失败，请以管理员身份运行后端",
            "results": results,
        }

    def _spawn(self, command: list[str], log_path: Path, cwd: Path | None = None) -> subprocess.Popen:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd or self.backend_dir),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=os.environ.copy(),
                creationflags=creationflags,
                shell=False,
                text=True,
            )
        finally:
            log_file.close()
        return process

    @staticmethod
    def _wait_port(host: str, port: int, timeout: float = 25.0):
        end = time.time() + timeout
        last_error = ""
        while time.time() < end:
            try:
                with socket.create_connection((host, int(port)), timeout=1.0):
                    return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.4)
        raise DaskClusterError(f"等待 {host}:{port} 启动超时。{last_error}")

    def _start_worker_process(
        self,
        scheduler_address: str,
        worker_name: str,
        nworkers: int,
        nthreads: int,
        memory_limit: str,
    ) -> int:
        local_dir = self.runtime_dir / "worker-space"
        local_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            "-m",
            "distributed.cli.dask_worker",
            scheduler_address,
            "--name",
            worker_name,
            "--nworkers",
            str(max(1, int(nworkers))),
            "--nthreads",
            str(max(1, int(nthreads))),
            "--memory-limit",
            str(memory_limit or "auto"),
            "--local-directory",
            str(local_dir),
            "--dashboard-address",
            ":0",
        ]
        process = self._spawn(cmd, self.worker_log, cwd=self.backend_dir)
        return int(process.pid)

    def start_head(
        self,
        bind_ip: str = "",
        scheduler_port: int = 8786,
        dashboard_port: int = 8787,
        api_port: int = 8000,
        worker_name: str = "",
        nworkers: int = 1,
        nthreads: int = 1,
        memory_limit: str = "auto",
        shared_runtime_root: str = "",
        auto_install: bool = True,
    ) -> dict:
        self.ensure_installed(auto_install=auto_install)
        self.stop_local_processes(clear_identity=False)

        node_ip = (bind_ip or self.local_ip()).strip()
        scheduler_port = int(scheduler_port or 8786)
        dashboard_port = int(dashboard_port or 8787)
        api_port = int(api_port or 8000)
        worker_name = (worker_name or f"{socket.gethostname()}-head").strip()

        self.scheduler_log.write_text("", encoding="utf-8")
        self.worker_log.write_text("", encoding="utf-8")

        scheduler_cmd = [
            sys.executable,
            "-m",
            "distributed.cli.dask_scheduler",
            "--host",
            "0.0.0.0",
            "--port",
            str(scheduler_port),
            "--dashboard-address",
            f":{dashboard_port}",
        ]
        scheduler_process = self._spawn(scheduler_cmd, self.scheduler_log, cwd=self.backend_dir)

        try:
            self._wait_port("127.0.0.1", scheduler_port, timeout=30)
            scheduler_address = f"tcp://{node_ip}:{scheduler_port}"
            worker_pid = self._start_worker_process(
                scheduler_address,
                worker_name,
                nworkers,
                nthreads,
                memory_limit,
            )

            cluster_id = uuid.uuid4().hex[:12]
            join_token = secrets.token_urlsafe(24)
            shared_path = str(shared_runtime_root or "").strip()
            if shared_path:
                Path(shared_path).mkdir(parents=True, exist_ok=True)

            with self.lock:
                self.state.update({
                    "role": "head",
                    "execution_mode": "distributed" if shared_path else "local",
                    "cluster_id": cluster_id,
                    "join_token": join_token,
                    "scheduler_address": scheduler_address,
                    "head_api_url": f"http://{node_ip}:{api_port}",
                    "scheduler_port": scheduler_port,
                    "dashboard_port": dashboard_port,
                    "api_port": api_port,
                    "scheduler_pid": int(scheduler_process.pid),
                    "worker_pid": int(worker_pid),
                    "worker_name": worker_name,
                    "nworkers": max(1, int(nworkers)),
                    "nthreads": max(1, int(nthreads)),
                    "memory_limit": str(memory_limit or "auto"),
                    "shared_runtime_root": shared_path,
                    "created_at": _now_iso(),
                    "joined_at": _now_iso(),
                    "last_error": "",
                })
            self._save_state()
            self.open_firewall(api_port, scheduler_port, dashboard_port)

            # 等待本机 Worker 注册。
            end = time.time() + 30
            while time.time() < end:
                try:
                    info = self._scheduler_info()
                    if info.get("workers"):
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            return self.status()
        except Exception:
            self._terminate_pid(scheduler_process.pid)
            raise

    def get_join_info(self, token: str) -> dict:
        with self.lock:
            expected = str(self.state.get("join_token") or "")
            role = str(self.state.get("role") or "")
            scheduler_address = str(self.state.get("scheduler_address") or "")
        if role != "head" or not scheduler_address:
            raise DaskClusterError("当前节点不是正在运行的主节点")
        if not token or not secrets.compare_digest(str(token), expected):
            raise DaskClusterError("集群加入令牌错误")
        return {
            "cluster_id": self.state.get("cluster_id"),
            "scheduler_address": scheduler_address,
            "head_api_url": self.state.get("head_api_url"),
            "package_spec": self.state.get("package_spec") or DASK_PACKAGE_SPEC,
            "shared_runtime_root": self.state.get("shared_runtime_root") or "",
            "dashboard_port": self.state.get("dashboard_port") or 8787,
        }

    @staticmethod
    def _request_join_info(head_ip: str, api_port: int, token: str) -> dict:
        host = str(head_ip or "").strip()
        if host.startswith("http://") or host.startswith("https://"):
            base = host.rstrip("/")
            # 用户可能输入 http://ip:8000
            parsed = urllib.parse.urlparse(base)
            if parsed.port is None:
                base = f"{base}:{int(api_port)}"
        else:
            base = f"http://{host}:{int(api_port)}"

        url = f"{base}/api/distributed/join-info?token={urllib.parse.quote(token)}"
        try:
            with urllib.request.urlopen(url, timeout=12) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except Exception as exc:
            raise DaskClusterError(
                f"无法从主节点获取集群信息：{url}\n{type(exc).__name__}: {exc}\n"
                "请确认主节点后端使用 --host 0.0.0.0 启动，并已开放 API 端口。"
            )

    def join_cluster(
        self,
        head_ip: str,
        api_port: int,
        join_token: str,
        worker_name: str = "",
        nworkers: int = 1,
        nthreads: int = 1,
        memory_limit: str = "auto",
        auto_install: bool = True,
    ) -> dict:
        join_info = self._request_join_info(head_ip, int(api_port or 8000), join_token)
        package_spec = str(join_info.get("package_spec") or DASK_PACKAGE_SPEC)
        self.state["package_spec"] = package_spec

        installed_version = self._package_version("distributed")
        required_version = package_spec.split("==", 1)[1] if "==" in package_spec else ""
        if not installed_version or (required_version and installed_version != required_version):
            if auto_install:
                self.install(package_spec=package_spec, upgrade=True)
            else:
                raise DaskClusterError(f"节点 Dask 版本不匹配，需要安装：{package_spec}")

        self.stop_local_processes(clear_identity=False)

        scheduler_address = str(join_info["scheduler_address"])
        worker_name = (worker_name or socket.gethostname()).strip()
        worker_pid = self._start_worker_process(
            scheduler_address,
            worker_name,
            nworkers,
            nthreads,
            memory_limit,
        )

        with self.lock:
            self.state.update({
                "role": "worker",
                "execution_mode": "local",
                "cluster_id": str(join_info.get("cluster_id") or ""),
                "join_token": "",
                "scheduler_address": scheduler_address,
                "head_api_url": str(join_info.get("head_api_url") or ""),
                "api_port": int(api_port or 8000),
                "scheduler_pid": None,
                "worker_pid": int(worker_pid),
                "worker_name": worker_name,
                "nworkers": max(1, int(nworkers)),
                "nthreads": max(1, int(nthreads)),
                "memory_limit": str(memory_limit or "auto"),
                "shared_runtime_root": str(join_info.get("shared_runtime_root") or ""),
                "joined_at": _now_iso(),
                "last_error": "",
            })
        self._save_state()

        # 等待 Worker 在 Scheduler 中出现。
        end = time.time() + 30
        while time.time() < end:
            try:
                info = self._scheduler_info()
                names = {str(v.get("name") or "") for v in (info.get("workers") or {}).values()}
                if worker_name in names or info.get("workers"):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        return self.status()

    def stop_local_processes(self, clear_identity: bool = True):
        self._close_client()
        worker_pid = self.state.get("worker_pid")
        scheduler_pid = self.state.get("scheduler_pid")

        if worker_pid:
            self._terminate_pid(worker_pid)
        if scheduler_pid:
            self._terminate_pid(scheduler_pid)

        with self.lock:
            self.state["worker_pid"] = None
            self.state["scheduler_pid"] = None
            self.state["execution_mode"] = "local"
            if clear_identity:
                self.state.update({
                    "role": "standalone",
                    "cluster_id": "",
                    "join_token": "",
                    "scheduler_address": "",
                    "head_api_url": "",
                    "created_at": "",
                    "joined_at": "",
                })
        self._save_state()

    def leave_cluster(self) -> dict:
        self.stop_local_processes(clear_identity=True)
        return self.status()

    def stop_cluster(self) -> dict:
        # 主节点停止 Scheduler 和本机 Worker。其他节点的 Worker 会断线并等待重连，
        # 用户可在各子节点页面点击“退出集群”彻底结束本地 Worker。
        self.stop_local_processes(clear_identity=True)
        return self.status()

    def set_execution_mode(self, mode: str, shared_runtime_root: str = "") -> dict:
        mode = str(mode or "").strip().lower()
        if mode not in {"local", "distributed"}:
            raise DaskClusterError("执行模式只能是 local 或 distributed")

        if shared_runtime_root is not None:
            shared = str(shared_runtime_root or "").strip()
            if shared:
                path = Path(shared)
                path.mkdir(parents=True, exist_ok=True)
                if not path.is_dir():
                    raise DaskClusterError(f"共享运行目录不可用：{shared}")
            self.state["shared_runtime_root"] = shared

        if mode == "distributed":
            if self.state.get("role") != "head":
                raise DaskClusterError("只有主节点可以启用分布式任务调度")
            info = self._scheduler_info()
            if not info.get("workers"):
                raise DaskClusterError("当前集群没有可用 Worker")
            if not str(self.state.get("shared_runtime_root") or "").strip():
                raise DaskClusterError(
                    "启用分布式任务前必须设置共享运行目录，推荐使用所有节点可访问的 UNC 路径，"
                    "例如 \\\\192.168.2.100\\local_web_runtime"
                )

        self.state["execution_mode"] = mode
        self._save_state()
        return self.status()

    def distributed_execution_enabled(self) -> bool:
        return (
            str(self.state.get("role") or "") == "head"
            and str(self.state.get("execution_mode") or "") == "distributed"
            and bool(self.state.get("scheduler_address"))
            and self._pid_alive(self.state.get("scheduler_pid"))
        )

    def get_shared_runtime_root(self) -> str:
        return str(self.state.get("shared_runtime_root") or "").strip()

    def get_client(self):
        self.ensure_installed(auto_install=False)
        address = str(self.state.get("scheduler_address") or "").strip()
        if not address:
            raise DaskClusterError("未配置 Dask Scheduler 地址")

        client = self._client
        if client is not None:
            try:
                client.scheduler_info()
                return client
            except Exception:
                self._close_client()

        from distributed import Client  # type: ignore

        self._client = Client(
            address,
            timeout="8s",
            set_as_default=False,
            direct_to_workers=False,
        )
        return self._client

    def _scheduler_info(self) -> dict:
        client = self.get_client()
        return client.scheduler_info(n_workers=-1)

    def test_shared_path(self, path_text: str = "") -> dict:
        path_text = str(path_text or self.state.get("shared_runtime_root") or "").strip()
        if not path_text:
            raise DaskClusterError("请先填写共享运行目录")
        local = _worker_shared_path_probe(path_text)
        client = self.get_client()
        remote = client.run(_worker_shared_path_probe, path_text)
        return {
            "path": path_text,
            "local": local,
            "workers": remote,
            "all_ready": bool(local.get("writable")) and all(
                bool(item.get("writable")) for item in remote.values()
            ),
        }

    def _node_resources(self) -> dict:
        memory_total = None
        memory_available = None
        disk_total = None
        disk_free = None
        try:
            import psutil  # type: ignore
            vm = psutil.virtual_memory()
            memory_total = _human_gb(vm.total)
            memory_available = _human_gb(vm.available)
            usage = shutil.disk_usage(str(self.backend_dir.anchor or self.backend_dir))
            disk_total = _human_gb(usage.total)
            disk_free = _human_gb(usage.free)
        except Exception:
            try:
                usage = shutil.disk_usage(str(self.backend_dir))
                disk_total = _human_gb(usage.total)
                disk_free = _human_gb(usage.free)
            except Exception:
                pass

        return {
            "hostname": socket.gethostname(),
            "ip": self.local_ip(),
            "os": platform.platform(),
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "cpu_count": int(os.cpu_count() or 1),
            "memory_total_gb": memory_total,
            "memory_available_gb": memory_available,
            "disk_total_gb": disk_total,
            "disk_free_gb": disk_free,
            "backend_dir": str(self.backend_dir),
            "project_root": str(self.project_root),
        }

    def status(self) -> dict:
        scheduler_alive = self._pid_alive(self.state.get("scheduler_pid"))
        worker_alive = self._pid_alive(self.state.get("worker_pid"))
        scheduler_online = False
        scheduler_error = ""
        workers: list[dict] = []

        if self.state.get("scheduler_address") and self.package_info()["installed"]:
            try:
                info = self._scheduler_info()
                scheduler_online = True
                for address, item in (info.get("workers") or {}).items():
                    metrics = item.get("metrics") or {}
                    workers.append({
                        "address": address,
                        "name": item.get("name") or address,
                        "host": item.get("host") or "",
                        "status": item.get("status") or "running",
                        "nthreads": item.get("nthreads") or 0,
                        "memory_limit_gb": _human_gb(item.get("memory_limit")),
                        "memory_used_gb": _human_gb(metrics.get("memory")),
                        "cpu_percent": metrics.get("cpu"),
                        "executing": metrics.get("executing"),
                        "last_seen": metrics.get("time"),
                    })
            except Exception as exc:
                scheduler_error = f"{type(exc).__name__}: {exc}"
                self._close_client()

        node_ip = self.local_ip()
        dashboard_port = int(self.state.get("dashboard_port") or 8787)
        role = str(self.state.get("role") or "standalone")

        return {
            "node": self._node_resources(),
            "package": self.package_info(),
            "role": role,
            "execution_mode": self.state.get("execution_mode") or "local",
            "cluster_id": self.state.get("cluster_id") or "",
            "join_token": self.state.get("join_token") if role == "head" else "",
            "scheduler_address": self.state.get("scheduler_address") or "",
            "head_api_url": self.state.get("head_api_url") or "",
            "shared_runtime_root": self.state.get("shared_runtime_root") or "",
            "scheduler_port": self.state.get("scheduler_port") or 8786,
            "dashboard_port": dashboard_port,
            "api_port": self.state.get("api_port") or 8000,
            "dashboard_url": (
                f"http://{node_ip}:{dashboard_port}/status"
                if role == "head" and scheduler_alive
                else ""
            ),
            "scheduler_pid": self.state.get("scheduler_pid"),
            "worker_pid": self.state.get("worker_pid"),
            "scheduler_alive": scheduler_alive,
            "worker_alive": worker_alive,
            "scheduler_online": scheduler_online,
            "scheduler_error": scheduler_error,
            "workers": workers,
            "worker_count": len(workers),
            "total_threads": sum(int(w.get("nthreads") or 0) for w in workers),
            "created_at": self.state.get("created_at") or "",
            "joined_at": self.state.get("joined_at") or "",
            "last_error": self.state.get("last_error") or "",
            "logs": self.tail_logs(),
        }

    @staticmethod
    def _tail_file(path: Path, max_chars: int = 12000) -> str:
        if not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            return text[-max_chars:]
        except Exception:
            return ""

    def tail_logs(self) -> dict:
        return {
            "scheduler": self._tail_file(self.scheduler_log),
            "worker": self._tail_file(self.worker_log),
            "install": self._tail_file(self.install_log),
        }
