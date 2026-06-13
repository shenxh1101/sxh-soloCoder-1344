import socket
import time
import threading
import urllib.request
import urllib.error
import http.client
from typing import Optional, Callable, Dict, Tuple

from .config import HealthCheckConfig, ServiceConfig
from .process_manager import ProcessManager, ServiceStatus, ServiceInstance
from .utils import colorize


class HealthCheckResult:
    def __init__(self, healthy: bool, reason: str = "", status_code: Optional[int] = None):
        self.healthy = healthy
        self.reason = reason
        self.status_code = status_code

    def __bool__(self):
        return self.healthy


class HealthChecker:
    def __init__(self, process_manager: ProcessManager, interval: int = 5):
        self.process_manager = process_manager
        self.interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._failed_counts: Dict[str, int] = {}
        self._last_failure_reason: Dict[str, str] = {}
        self._lock = threading.Lock()
        self.on_unhealthy: Optional[Callable[[str, str], None]] = None
        self.on_healthy: Optional[Callable[[str], None]] = None
        self.on_restart: Optional[Callable[[str, int, str], None]] = None
        self.on_max_restarts: Optional[Callable[[str, int, str], None]] = None

    def _check_tcp(self, host: str, port: int, timeout: int) -> HealthCheckResult:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            start = time.time()
            result = sock.connect_ex((host, port))
            elapsed = (time.time() - start) * 1000
            sock.close()

            if result == 0:
                return HealthCheckResult(True, f"TCP连接成功 ({elapsed:.0f}ms)")
            else:
                err_msg = {
                    10061: "连接被拒绝 (端口未监听)",
                    10060: "连接超时",
                    10065: "主机不可达",
                    113: "无路由到主机",
                    111: "连接被拒绝",
                    110: "连接超时",
                }.get(result, f"错误码={result}")
                return HealthCheckResult(False, f"TCP连接失败: {err_msg}")
        except socket.timeout:
            return HealthCheckResult(False, f"TCP连接超时 ({timeout}s)")
        except socket.error as e:
            return HealthCheckResult(False, f"TCP连接异常: {str(e)}")
        except Exception as e:
            return HealthCheckResult(False, f"TCP探测错误: {type(e).__name__}: {str(e)}")

    def _check_http(self, host: str, port: int, path: str, timeout: int) -> HealthCheckResult:
        url = f"http://{host}:{port}{path}"
        try:
            req = urllib.request.Request(url, method="GET", headers={
                "User-Agent": "mservice-health-checker/1.0",
                "Accept": "*/*",
            })
            start = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                elapsed = (time.time() - start) * 1000
                status = resp.status

                if 200 <= status < 300:
                    return HealthCheckResult(
                        True,
                        f"HTTP {status} OK ({elapsed:.0f}ms)",
                        status_code=status,
                    )
                elif 300 <= status < 400:
                    return HealthCheckResult(
                        False,
                        f"HTTP重定向 {status} (需要2xx状态码)",
                        status_code=status,
                    )
                elif 400 <= status < 500:
                    client_errors = {
                        400: "请求错误",
                        401: "未授权",
                        403: "禁止访问",
                        404: "页面不存在",
                        405: "方法不允许",
                        408: "请求超时",
                        429: "请求过多",
                    }
                    err_desc = client_errors.get(status, "客户端错误")
                    return HealthCheckResult(
                        False,
                        f"HTTP {status} {err_desc} (页面异常)",
                        status_code=status,
                    )
                else:
                    server_errors = {
                        500: "服务器内部错误",
                        502: "网关错误",
                        503: "服务不可用",
                        504: "网关超时",
                    }
                    err_desc = server_errors.get(status, "服务器错误")
                    return HealthCheckResult(
                        False,
                        f"HTTP {status} {err_desc} (服务端异常)",
                        status_code=status,
                    )
        except urllib.error.HTTPError as e:
            elapsed = (time.time() - start) * 1000 if 'start' in locals() else 0
            status = e.code
            if 400 <= status < 500:
                client_errors = {
                    400: "请求错误",
                    401: "未授权",
                    403: "禁止访问",
                    404: "页面不存在",
                    405: "方法不允许",
                    408: "请求超时",
                    429: "请求过多",
                }
                err_desc = client_errors.get(status, "客户端错误")
                return HealthCheckResult(
                    False,
                    f"HTTP {status} {err_desc} (页面挂了)",
                    status_code=status,
                )
            else:
                server_errors = {
                    500: "服务器内部错误",
                    502: "网关错误",
                    503: "服务不可用",
                    504: "网关超时",
                }
                err_desc = server_errors.get(status, "服务器错误")
                return HealthCheckResult(
                    False,
                    f"HTTP {status} {err_desc}",
                    status_code=status,
                )
        except urllib.error.URLError as e:
            reason = str(e.reason)
            if "timed out" in reason or "timeout" in reason.lower():
                return HealthCheckResult(False, f"HTTP请求超时 ({timeout}s)")
            elif "refused" in reason or "10061" in reason or "111" in reason:
                return HealthCheckResult(False, f"HTTP连接失败: 端口未监听/连接被拒绝")
            elif "unreachable" in reason or "10065" in reason:
                return HealthCheckResult(False, f"HTTP连接失败: 主机不可达")
            elif "Name or service not known" in reason or "getaddrinfo failed" in reason:
                return HealthCheckResult(False, f"HTTP连接失败: 域名解析失败")
            else:
                return HealthCheckResult(False, f"HTTP连接失败: {reason}")
        except socket.timeout:
            return HealthCheckResult(False, f"HTTP请求超时 ({timeout}s)")
        except http.client.RemoteDisconnected:
            return HealthCheckResult(False, "HTTP连接被远端关闭 (服务可能崩溃)")
        except ConnectionRefusedError:
            return HealthCheckResult(False, "HTTP连接被拒绝 (端口未监听/服务未启动)")
        except ConnectionResetError:
            return HealthCheckResult(False, "HTTP连接被重置 (服务异常中断)")
        except OSError as e:
            err_num = getattr(e, 'winerror', None) or getattr(e, 'errno', None)
            if err_num in (10061, 111):
                return HealthCheckResult(False, "HTTP连接失败: 连接被拒绝")
            elif err_num in (10060, 110):
                return HealthCheckResult(False, f"HTTP连接超时 ({timeout}s)")
            else:
                return HealthCheckResult(False, f"HTTP连接OS错误: {str(e)}")
        except Exception as e:
            return HealthCheckResult(False, f"HTTP探测异常: {type(e).__name__}: {str(e)}")

    def check_service(self, svc: ServiceInstance) -> HealthCheckResult:
        cfg = svc.config.health_check

        if cfg is None:
            if svc.is_alive():
                return HealthCheckResult(True, "进程存活 (无健康检查配置)")
            else:
                return HealthCheckResult(False, "进程已退出")

        if not svc.is_alive():
            exit_code = svc.exit_code
            if exit_code is not None and exit_code != 0:
                return HealthCheckResult(False, f"进程已异常退出 (退出码: {exit_code})")
            else:
                return HealthCheckResult(False, "进程已退出")

        host = "127.0.0.1"
        port = cfg.port
        if port is None and svc.config.ports:
            port = svc.config.ports[0].host
        if port is None:
            return HealthCheckResult(True, "进程存活 (未配置检查端口)")

        if cfg.type == "http":
            return self._check_http(host, port, cfg.path, cfg.timeout)
        else:
            return self._check_tcp(host, port, cfg.timeout)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_all()
            except Exception as e:
                print(colorize(f"[健康检查] 循环异常: {type(e).__name__}: {e}", "bright_red"))
            self._stop_event.wait(self.interval)

    def _check_all(self) -> None:
        for name, svc in self.process_manager.services.items():
            svc.poll()

            if svc.status in (ServiceStatus.STOPPED, ServiceStatus.ERROR):
                continue

            if svc.status == ServiceStatus.STARTING:
                grace_period = 10
                if svc.start_time and (time.time() - svc.start_time) < grace_period:
                    continue

            result = self.check_service(svc)
            self._last_failure_reason[name] = result.reason

            with self._lock:
                if result.healthy:
                    self._failed_counts[name] = 0
                    if svc.status != ServiceStatus.RUNNING:
                        svc.status = ServiceStatus.RUNNING
                        if self.on_healthy:
                            self.on_healthy(name)
                else:
                    self._failed_counts[name] = self._failed_counts.get(name, 0) + 1
                    svc.status = ServiceStatus.UNHEALTHY

                    failed_count = self._failed_counts[name]
                    hc_cfg = svc.config.health_check
                    retries_before_restart = hc_cfg.retries if hc_cfg else 3

                    if self.on_unhealthy:
                        self.on_unhealthy(name, result.reason, failed_count, retries_before_restart)

                    if failed_count >= retries_before_restart:
                        self._try_restart(name, svc, result.reason)

    def _try_restart(self, name: str, svc: ServiceInstance, last_reason: str) -> None:
        max_restarts = svc.config.max_restarts

        if svc.restart_count >= max_restarts:
            svc.status = ServiceStatus.ERROR
            if self.on_max_restarts:
                self.on_max_restarts(name, max_restarts, last_reason)
            return

        next_attempt = svc.restart_count + 1

        if self.on_restart:
            self.on_restart(name, next_attempt, max_restarts, last_reason)

        try:
            svc.restart_count += 1
            svc.restart()
            with self._lock:
                self._failed_counts[name] = 0
        except Exception as e:
            err = f"重启失败: {type(e).__name__}: {str(e)}"
            if svc.restart_count >= max_restarts:
                svc.status = ServiceStatus.ERROR
                if self.on_max_restarts:
                    self.on_max_restarts(name, max_restarts, err)

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

    def get_last_failure_reason(self, name: str) -> str:
        return self._last_failure_reason.get(name, "")

    def reset_failed_count(self, name: str) -> None:
        with self._lock:
            self._failed_counts[name] = 0
