import socket
import time
import threading
import urllib.request
import urllib.error
from typing import Optional, Callable, Dict

from .config import HealthCheckConfig, ServiceConfig
from .process_manager import ProcessManager, ServiceStatus, ServiceInstance


class HealthChecker:
    def __init__(self, process_manager: ProcessManager, interval: int = 5):
        self.process_manager = process_manager
        self.interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._failed_counts: Dict[str, int] = {}
        self.on_unhealthy: Optional[Callable[[str], None]] = None
        self.on_healthy: Optional[Callable[[str], None]] = None
        self.on_restart: Optional[Callable[[str, int], None]] = None
        self.on_max_restarts: Optional[Callable[[str], None]] = None

    def _check_tcp(self, host: str, port: int, timeout: int) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception:
            return False

    def _check_http(self, host: str, port: int, path: str, timeout: int) -> bool:
        try:
            url = f"http://{host}:{port}{path}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 500
        except Exception:
            return False

    def check_service(self, svc: ServiceInstance) -> bool:
        cfg = svc.config.health_check
        if cfg is None:
            return True

        if not svc.is_alive():
            return False

        host = "127.0.0.1"
        port = cfg.port
        if port is None and svc.config.ports:
            port = svc.config.ports[0].host
        if port is None:
            return True

        if cfg.type == "http":
            return self._check_http(host, port, cfg.path, cfg.timeout)
        else:
            return self._check_tcp(host, port, cfg.timeout)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_all()
            except Exception:
                pass
            self._stop_event.wait(self.interval)

    def _check_all(self) -> None:
        for name, svc in self.process_manager.services.items():
            svc.poll()

            if svc.status in (ServiceStatus.STOPPED, ServiceStatus.ERROR):
                continue

            if svc.status == ServiceStatus.STARTING:
                if svc.start_time and (time.time() - svc.start_time) < 3:
                    continue

            is_healthy = self.check_service(svc)

            if is_healthy:
                self._failed_counts[name] = 0
                if svc.status != ServiceStatus.RUNNING:
                    svc.status = ServiceStatus.RUNNING
                    if self.on_healthy:
                        self.on_healthy(name)
            else:
                self._failed_counts[name] = self._failed_counts.get(name, 0) + 1
                svc.status = ServiceStatus.UNHEALTHY

                if self.on_unhealthy:
                    self.on_unhealthy(name)

                hc_cfg = svc.config.health_check
                retries = hc_cfg.retries if hc_cfg else 3
                if self._failed_counts[name] >= retries:
                    self._try_restart(name, svc)

    def _try_restart(self, name: str, svc: ServiceInstance) -> None:
        if svc.restart_count >= svc.config.max_restarts:
            if self.on_max_restarts:
                self.on_max_restarts(name)
            return

        if self.on_restart:
            self.on_restart(name, svc.restart_count + 1)

        try:
            svc.restart_count += 1
            svc.restart()
            self._failed_counts[name] = 0
        except Exception:
            pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def get_failed_count(self, name: str) -> int:
        return self._failed_counts.get(name, 0)

    def reset_failed_count(self, name: str) -> None:
        self._failed_counts[name] = 0
