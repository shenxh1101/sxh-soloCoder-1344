import os
import time
import threading
from typing import List, Optional, Set, Callable, TextIO

from .config import AppConfig
from .process_manager import ProcessManager
from .utils import colorize


class LogAggregator:
    def __init__(self, app_config: AppConfig, process_manager: ProcessManager):
        self.app_config = app_config
        self.process_manager = process_manager
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._file_pointers = {}
        self._output_lock = threading.Lock()
        self._filters: Set[str] = set()
        self._keyword: Optional[str] = None
        self._on_log: Optional[Callable[[str, str], None]] = None

    def set_filters(self, service_names: List[str]) -> None:
        self._filters = set(service_names)

    def set_keyword(self, keyword: Optional[str]) -> None:
        self._keyword = keyword

    def set_on_log(self, callback: Optional[Callable[[str, str], None]]) -> None:
        self._on_log = callback

    def _open_log_files(self) -> None:
        for svc_cfg in self.app_config.services:
            log_file = os.path.join(self.app_config.global_config.log_dir, f"{svc_cfg.name}.log")
            if os.path.exists(log_file):
                fp = open(log_file, "r", encoding="utf-8", errors="replace")
                fp.seek(0, os.SEEK_END)
                self._file_pointers[svc_cfg.name] = fp

    def _close_log_files(self) -> None:
        for fp in self._file_pointers.values():
            try:
                fp.close()
            except Exception:
                pass
        self._file_pointers.clear()

    def _format_line(self, service_name: str, line: str) -> str:
        svc = self.app_config.get_service(service_name)
        color = svc.color if svc else "white"
        timestamp = time.strftime("%H:%M:%S")
        name_colored = colorize(f"[{service_name:10s}]", color, bold=True)
        time_colored = colorize(f"[{timestamp}]", "bright_black")
        return f"{time_colored} {name_colored} {line.rstrip()}"

    def _should_show(self, service_name: str, line: str) -> bool:
        if self._filters and service_name not in self._filters:
            return False
        if self._keyword and self._keyword.lower() not in line.lower():
            return False
        return True

    def _tail_loop(self) -> None:
        self._open_log_files()
        try:
            while not self._stop_event.is_set():
                for svc_cfg in self.app_config.services:
                    name = svc_cfg.name
                    fp = self._file_pointers.get(name)
                    if fp is None:
                        log_file = os.path.join(
                            self.app_config.global_config.log_dir, f"{name}.log"
                        )
                        if os.path.exists(log_file):
                            fp = open(log_file, "r", encoding="utf-8", errors="replace")
                            fp.seek(0, os.SEEK_END)
                            self._file_pointers[name] = fp
                        else:
                            continue

                    try:
                        lines = fp.readlines()
                    except Exception:
                        continue

                    for line in lines:
                        if not line.strip():
                            continue
                        if self._should_show(name, line):
                            formatted = self._format_line(name, line)
                            if self._on_log:
                                self._on_log(name, line.rstrip())
                            with self._output_lock:
                                print(formatted, flush=True)

                time.sleep(0.2)
        finally:
            self._close_log_files()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._tail_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def follow(self, service_names: Optional[List[str]] = None,
               keyword: Optional[str] = None) -> None:
        if service_names:
            self.set_filters(service_names)
        if keyword:
            self.set_keyword(keyword)

        try:
            self.start()
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def print_history(self, service_names: Optional[List[str]] = None,
                      keyword: Optional[str] = None, lines: int = 100) -> None:
        for svc_cfg in self.app_config.services:
            if service_names and svc_cfg.name not in service_names:
                continue

            log_file = os.path.join(
                self.app_config.global_config.log_dir, f"{svc_cfg.name}.log"
            )
            if not os.path.exists(log_file):
                continue

            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            if keyword:
                all_lines = [l for l in all_lines if keyword.lower() in l.lower()]

            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            for line in tail:
                if not line.strip():
                    continue
                formatted = self._format_line(svc_cfg.name, line)
                print(formatted)
