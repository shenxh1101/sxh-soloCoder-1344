import os
import subprocess
import time
import threading
import signal
from typing import List, Dict, Optional, Callable
from enum import Enum
from collections import deque

from .config import ServiceConfig, AppConfig
from .utils import ensure_dir, expand_env


CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008


class ServiceStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    UNHEALTHY = "unhealthy"
    ERROR = "error"
    EXITED = "exited"


class ServiceInstance:
    def __init__(self, config: ServiceConfig, log_dir: str):
        self.config = config
        self.log_dir = log_dir
        self.process: Optional[subprocess.Popen] = None
        self.status: ServiceStatus = ServiceStatus.STOPPED
        self.pid: Optional[int] = None
        self.restart_count: int = 0
        self.start_time: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.log_file: str = os.path.join(log_dir, f"{config.name}.log")
        self._log_fp = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self.status in (ServiceStatus.RUNNING, ServiceStatus.STARTING):
                return False

            ensure_dir(os.path.dirname(self.log_file))
            self._log_fp = open(self.log_file, "a", encoding="utf-8", buffering=1)

            env = expand_env(self.config.env)
            cwd = os.path.abspath(self.config.repo_path)

            env["PYTHONUNBUFFERED"] = "1"

            try:
                kwargs = dict(
                    args=self.config.command,
                    cwd=cwd,
                    env=env,
                    stdout=self._log_fp,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )

                if os.name == "nt":
                    kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
                    if hasattr(subprocess, "CREATE_NO_WINDOW"):
                        kwargs["creationflags"] |= subprocess.CREATE_NO_WINDOW
                else:
                    kwargs["start_new_session"] = True

                self.process = subprocess.Popen(**kwargs)
            except Exception:
                self.status = ServiceStatus.ERROR
                if self._log_fp:
                    self._log_fp.close()
                    self._log_fp = None
                raise

            self.pid = self.process.pid
            self.status = ServiceStatus.STARTING
            self.start_time = time.time()
            self.exit_code = None
            return True

    def stop(self, force: bool = False) -> Optional[int]:
        with self._lock:
            if self.process is not None:
                try:
                    if force:
                        self.process.kill()
                    else:
                        self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
                except Exception:
                    pass

                self.exit_code = self.process.returncode
                self.status = ServiceStatus.STOPPED
                self.pid = None
                self.process = None

                if self._log_fp:
                    try:
                        self._log_fp.close()
                    except Exception:
                        pass
                    self._log_fp = None

                return self.exit_code
            elif self.pid is not None:
                try:
                    if os.name == "nt":
                        sig = signal.SIGTERM if not force else signal.SIGKILL
                        os.kill(self.pid, sig)
                    else:
                        if force:
                            os.kill(self.pid, signal.SIGKILL)
                        else:
                            os.kill(self.pid, signal.SIGTERM)

                    import time as _time
                    for _ in range(50):
                        if not self._is_pid_alive(self.pid):
                            break
                        _time.sleep(0.1)
                except Exception:
                    pass

                self.exit_code = -1
                self.status = ServiceStatus.STOPPED
                self.pid = None

                if self._log_fp:
                    try:
                        self._log_fp.close()
                    except Exception:
                        pass
                    self._log_fp = None

                return self.exit_code
            else:
                return self.exit_code

    def restart(self) -> bool:
        self.stop()
        time.sleep(0.5)
        return self.start()

    def _is_pid_alive(self, pid: int) -> bool:
        if pid is None:
            return False
        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_INFORMATION = 0x0400
                process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
                if process:
                    exit_code = ctypes.c_ulong()
                    result = kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))
                    kernel32.CloseHandle(process)
                    if result:
                        STILL_ACTIVE = 259
                        return exit_code.value == STILL_ACTIVE
                return False
            except Exception:
                return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    def poll(self) -> Optional[int]:
        with self._lock:
            if self.process is not None:
                code = self.process.poll()
                if code is not None:
                    self.exit_code = code
                    if self.status not in (ServiceStatus.STOPPED,):
                        self.status = ServiceStatus.EXITED
                    self.pid = None
                    if self._log_fp:
                        try:
                            self._log_fp.close()
                        except Exception:
                            pass
                        self._log_fp = None
                return code
            else:
                if self.pid is not None:
                    if not self._is_pid_alive(self.pid):
                        if self.status not in (ServiceStatus.STOPPED,):
                            self.status = ServiceStatus.EXITED
                        self.pid = None
                        return self.exit_code
                return None

    def is_alive(self) -> bool:
        if self.process is not None:
            return self.poll() is None
        elif self.pid is not None:
            return self._is_pid_alive(self.pid)
        return False

    def cleanup(self) -> None:
        if self._log_fp:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None


class ProcessManager:
    def __init__(self, app_config: AppConfig):
        self.app_config = app_config
        self.services: Dict[str, ServiceInstance] = {}
        self._init_services()

    def _init_services(self) -> None:
        log_dir = self.app_config.global_config.log_dir
        ensure_dir(log_dir)
        for svc_cfg in self.app_config.services:
            self.services[svc_cfg.name] = ServiceInstance(svc_cfg, log_dir)

    def topological_sort(self) -> List[str]:
        visited = set()
        order = []
        visiting = set()

        def dfs(name: str) -> None:
            if name in visiting:
                raise ValueError(f"Circular dependency detected involving {name}")
            if name in visited:
                return
            visiting.add(name)
            svc = self.app_config.get_service(name)
            if svc:
                for dep in svc.dependencies:
                    dfs(dep)
            visited.add(name)
            order.append(name)
            visiting.discard(name)

        for svc in self.app_config.services:
            if svc.name not in visited:
                dfs(svc.name)

        return order

    def start_service(self, name: str) -> bool:
        svc = self.services.get(name)
        if not svc:
            raise ValueError(f"Service not found: {name}")
        return svc.start()

    def stop_service(self, name: str, force: bool = False) -> Optional[int]:
        svc = self.services.get(name)
        if not svc:
            raise ValueError(f"Service not found: {name}")
        return svc.stop(force=force)

    def restart_service(self, name: str) -> bool:
        svc = self.services.get(name)
        if not svc:
            raise ValueError(f"Service not found: {name}")
        svc.restart_count += 1
        return svc.restart()

    def start_all(self, on_start: Optional[Callable[[str], None]] = None) -> None:
        order = self.topological_sort()
        for name in order:
            if on_start:
                on_start(name)
            self.start_service(name)
            time.sleep(0.3)

    def stop_all(self, force: bool = False) -> None:
        order = list(reversed(self.topological_sort()))
        for name in order:
            self.stop_service(name, force=force)

    def get_status(self, name: str) -> ServiceStatus:
        svc = self.services.get(name)
        if not svc:
            raise ValueError(f"Service not found: {name}")
        svc.poll()
        return svc.status

    def get_all_status(self) -> Dict[str, ServiceStatus]:
        result = {}
        for name in self.services:
            result[name] = self.get_status(name)
        return result

    def get_instance(self, name: str) -> Optional[ServiceInstance]:
        return self.services.get(name)

    def cleanup_all(self) -> None:
        for svc in self.services.values():
            svc.cleanup()
