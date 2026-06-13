import os
import json
import time
import signal
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict, field

from .config import AppConfig
from .process_manager import ProcessManager, ServiceStatus


@dataclass
class ServiceSnapshot:
    name: str
    pid: Optional[int]
    status: str
    log_file: str
    restart_count: int
    start_time: Optional[float]
    exit_code: Optional[int]


@dataclass
class Snapshot:
    timestamp: float
    services: Dict[str, ServiceSnapshot] = field(default_factory=dict)
    config_path: str = ""


class SnapshotManager:
    def __init__(self, app_config: AppConfig, process_manager: ProcessManager):
        self.app_config = app_config
        self.process_manager = process_manager
        self._snapshot_file = os.path.abspath(app_config.global_config.snapshot_file)

    def _is_process_alive(self, pid: int) -> bool:
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
                try:
                    result = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    return str(pid) in result.stdout
                except Exception:
                    return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    def create_snapshot(self) -> Snapshot:
        services = {}
        for name, svc in self.process_manager.services.items():
            svc.poll()
            services[name] = ServiceSnapshot(
                name=name,
                pid=svc.pid,
                status=svc.status.value if isinstance(svc.status, ServiceStatus) else str(svc.status),
                log_file=svc.log_file,
                restart_count=svc.restart_count,
                start_time=svc.start_time,
                exit_code=svc.exit_code,
            )

        snapshot = Snapshot(
            timestamp=time.time(),
            services=services,
            config_path="",
        )
        return snapshot

    def save_snapshot(self, snapshot: Optional[Snapshot] = None) -> str:
        if snapshot is None:
            snapshot = self.create_snapshot()

        snapshot_dict = {
            "timestamp": snapshot.timestamp,
            "config_path": snapshot.config_path,
            "services": {
                name: asdict(ss) for name, ss in snapshot.services.items()
            },
        }

        os.makedirs(os.path.dirname(self._snapshot_file), exist_ok=True)
        with open(self._snapshot_file, "w", encoding="utf-8") as f:
            json.dump(snapshot_dict, f, indent=2, ensure_ascii=False)

        return self._snapshot_file

    def load_snapshot(self) -> Optional[Snapshot]:
        if not os.path.exists(self._snapshot_file):
            return None

        try:
            with open(self._snapshot_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            services = {}
            for name, svc_data in data.get("services", {}).items():
                services[name] = ServiceSnapshot(
                    name=svc_data["name"],
                    pid=svc_data.get("pid"),
                    status=svc_data.get("status", "stopped"),
                    log_file=svc_data.get("log_file", ""),
                    restart_count=svc_data.get("restart_count", 0),
                    start_time=svc_data.get("start_time"),
                    exit_code=svc_data.get("exit_code"),
                )

            snapshot = Snapshot(
                timestamp=data.get("timestamp", 0),
                services=services,
                config_path=data.get("config_path", ""),
            )
            return snapshot
        except Exception:
            return None

    def has_snapshot(self) -> bool:
        return os.path.exists(self._snapshot_file)

    def clear_snapshot(self) -> None:
        if os.path.exists(self._snapshot_file):
            os.remove(self._snapshot_file)

    def recover_from_snapshot(self) -> Dict[str, str]:
        snapshot = self.load_snapshot()
        if not snapshot:
            return {}

        recovered = {}
        for name, ss in snapshot.services.items():
            inst = self.process_manager.get_instance(name)
            if not inst:
                continue

            if ss.pid and self._is_process_alive(ss.pid):
                inst.pid = ss.pid
                inst.status = ServiceStatus.RUNNING
                inst.restart_count = ss.restart_count
                inst.start_time = ss.start_time
                inst.log_file = ss.log_file
                recovered[name] = "recovered"
            else:
                inst.status = ServiceStatus.EXITED
                inst.exit_code = ss.exit_code
                inst.restart_count = ss.restart_count
                recovered[name] = "not_running"

        return recovered

    def get_running_services(self) -> List[str]:
        snapshot = self.load_snapshot()
        if not snapshot:
            return []

        running = []
        for name, ss in snapshot.services.items():
            if ss.pid and self._is_process_alive(ss.pid):
                running.append(name)
        return running

    def stop_all_from_snapshot(self, force: bool = False) -> Dict[str, str]:
        snapshot = self.load_snapshot()
        if not snapshot:
            return {}

        results = {}
        for name, ss in snapshot.services.items():
            if not ss.pid:
                results[name] = "no_pid"
                continue

            if not self._is_process_alive(ss.pid):
                results[name] = "not_running"
                continue

            try:
                if force:
                    os.kill(ss.pid, signal.SIGKILL)
                else:
                    os.kill(ss.pid, signal.SIGTERM)
                results[name] = "stopped"
            except ProcessLookupError:
                results[name] = "not_found"
            except Exception as e:
                results[name] = f"error: {e}"

        return results
