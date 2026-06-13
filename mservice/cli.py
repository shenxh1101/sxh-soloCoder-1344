import os
import sys
import argparse
import time
import signal
import subprocess
from typing import List, Optional

from .config import load_config, AppConfig, ConfigValidationError, CircularDependencyError
from .process_manager import ProcessManager, ServiceStatus
from .health_checker import HealthChecker
from .log_aggregator import LogAggregator
from .topology import TopologyVisualizer
from .snapshot import SnapshotManager
from .utils import colorize, ensure_dir


DEFAULT_CONFIG_PATH = "./mservice.yaml"


class MServiceCLI:
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = os.path.abspath(config_path)
        self.app_config: Optional[AppConfig] = None
        self.process_manager: Optional[ProcessManager] = None
        self.health_checker: Optional[HealthChecker] = None
        self.log_aggregator: Optional[LogAggregator] = None
        self.visualizer: Optional[TopologyVisualizer] = None
        self.snapshot_manager: Optional[SnapshotManager] = None
        self._running = False

    def _load(self) -> None:
        if self.app_config is None:
            self.app_config = load_config(self.config_path)
            self.process_manager = ProcessManager(self.app_config)
            self.health_checker = HealthChecker(
                self.process_manager,
                interval=self.app_config.global_config.health_check_interval,
            )
            self.log_aggregator = LogAggregator(self.app_config, self.process_manager)
            self.visualizer = TopologyVisualizer(self.app_config, self.process_manager)
            self.snapshot_manager = SnapshotManager(self.app_config, self.process_manager)

    def cmd_start(self, args: argparse.Namespace) -> int:
        self._load()

        if args.recover and self.snapshot_manager.has_snapshot():
            print(colorize("正在从快照恢复...", "yellow"))
            recovered = self.snapshot_manager.recover_from_snapshot()
            for name, status in recovered.items():
                if status == "recovered":
                    print(f"  {colorize(name, 'green')}: 已恢复")
                else:
                    print(f"  {colorize(name, 'red')}: {status}")

        if args.services:
            services = args.services
            for name in services:
                if not self.app_config.get_service(name):
                    print(colorize(f"错误: 服务 '{name}' 不存在", "red"))
                    return 1
        else:
            services = self.process_manager.topological_sort()

        print(colorize("启动服务...", "cyan", bold=True))

        order = self.process_manager.topological_sort()
        start_order = [n for n in order if n in services]

        for name in start_order:
            inst = self.process_manager.get_instance(name)
            if inst and inst.is_alive():
                print(f"  {colorize(name, 'green')}: 已在运行")
                continue

            svc = self.app_config.get_service(name)
            try:
                self.process_manager.start_service(name)
                print(f"  {colorize(name, svc.color)}: 启动中...")
            except Exception as e:
                print(f"  {colorize(name, 'red')}: 启动失败 - {e}")
            time.sleep(0.2)

        if args.no_health_check:
            print(colorize("健康检查已禁用", "yellow"))
        else:
            self._setup_health_check_handlers()
            self.health_checker.start()
            print(colorize("健康检查已启用", "green"))

        if args.follow:
            print()
            print(colorize("日志模式: 按 Ctrl+C 退出", "bright_black"))
            print()
            try:
                self.log_aggregator.start()
                self._running = True
                while self._running:
                    time.sleep(1)
                    self._save_snapshot_safe()
            except KeyboardInterrupt:
                pass
            finally:
                self._shutdown()
        else:
            self._save_snapshot_safe()
            print()
            print(self.visualizer.render_status_table())

        return 0

    def _setup_health_check_handlers(self) -> None:
        def on_unhealthy(name: str, reason: str, failed_count: int, retries: int) -> None:
            svc = self.app_config.get_service(name)
            svc_color = svc.color if svc else "white"
            name_colored = colorize(name, svc_color, bold=True)
            progress = f"[{failed_count}/{retries}]"
            print(
                colorize(
                    f"⚠  {name_colored} 不健康 {progress}: {reason}",
                    "yellow",
                )
            )

        def on_healthy(name: str) -> None:
            svc = self.app_config.get_service(name)
            svc_color = svc.color if svc else "white"
            name_colored = colorize(name, svc_color, bold=True)
            print(colorize(f"✓  {name_colored} 健康检查通过", "green"))

        def on_restart(name: str, attempt: int, max_restarts: int, reason: str) -> None:
            svc = self.app_config.get_service(name)
            svc_color = svc.color if svc else "white"
            name_colored = colorize(name, svc_color, bold=True)
            progress = f"(第 {attempt}/{max_restarts} 次)"
            print(
                colorize(
                    f"↻  正在重启 {name_colored} {progress}，原因: {reason}",
                    "yellow",
                )
            )

        def on_max_restarts(name: str, max_restarts: int, reason: str) -> None:
            svc = self.app_config.get_service(name)
            svc_color = svc.color if svc else "white"
            name_colored = colorize(name, svc_color, bold=True)
            print()
            print(
                colorize(
                    f"✗  {name_colored} 已达到最大重启次数 ({max_restarts}次)，停止自动重启",
                    "bright_red",
                    bold=True,
                )
            )
            print(
                colorize(
                    f"   最后失败原因: {reason}",
                    "red",
                )
            )
            print(
                colorize(
                    f"   请手动排查问题后使用 'restart {name}' 命令重新启动",
                    "bright_black",
                )
            )
            print()

        self.health_checker.on_unhealthy = on_unhealthy
        self.health_checker.on_healthy = on_healthy
        self.health_checker.on_restart = on_restart
        self.health_checker.on_max_restarts = on_max_restarts

    def _save_snapshot_safe(self) -> None:
        try:
            if self.snapshot_manager:
                self.snapshot_manager.save_snapshot()
        except Exception:
            pass

    def _shutdown(self) -> None:
        self._running = False
        if self.health_checker:
            self.health_checker.stop()
        if self.log_aggregator:
            self.log_aggregator.stop()
        self._save_snapshot_safe()

    def cmd_stop(self, args: argparse.Namespace) -> int:
        self._load()

        if args.from_snapshot:
            print(colorize("从快照停止服务...", "yellow"))
            results = self.snapshot_manager.stop_all_from_snapshot(force=args.force)
            for name, status in results.items():
                status_color = "green" if status == "stopped" else "yellow"
                print(f"  {colorize(name, 'cyan')}: {colorize(status, status_color)}")
            self.snapshot_manager.clear_snapshot()
            return 0

        if self.snapshot_manager.has_snapshot():
            self.snapshot_manager.recover_from_snapshot()

        if args.services:
            services = args.services
            for name in services:
                if not self.app_config.get_service(name):
                    print(colorize(f"错误: 服务 '{name}' 不存在", "red"))
                    return 1
        else:
            services = list(reversed(self.process_manager.topological_sort()))

        print(colorize("停止服务...", "cyan", bold=True))

        for name in services:
            inst = self.process_manager.get_instance(name)
            if not inst or not inst.is_alive():
                print(f"  {colorize(name, 'bright_black')}: 未运行")
                continue

            try:
                self.process_manager.stop_service(name, force=args.force)
                print(f"  {colorize(name, 'yellow')}: 已停止")
            except Exception as e:
                print(f"  {colorize(name, 'red')}: 停止失败 - {e}")

        self.snapshot_manager.clear_snapshot()
        return 0

    def cmd_status(self, args: argparse.Namespace) -> int:
        self._load()

        if self.snapshot_manager.has_snapshot():
            self.snapshot_manager.recover_from_snapshot()

        if args.topo:
            print(self.visualizer.render_simple())
            print()
            print(self._render_detailed_status())
        else:
            print(self.visualizer.render_status_table())
            print(self._render_detailed_status())

        return 0

    def _render_detailed_status(self) -> str:
        lines = []
        has_issues = False

        for svc_cfg in self.app_config.services:
            name = svc_cfg.name
            inst = self.process_manager.get_instance(name)
            if inst is None:
                continue

            status = inst.status
            hc_cfg = svc_cfg.health_check
            hc_reason = self.health_checker.get_last_failure_reason(name)
            failed_count = self.health_checker.get_failed_count(name)

            if status == ServiceStatus.UNHEALTHY and hc_reason:
                has_issues = True
                svc_color = svc_cfg.color
                name_colored = colorize(name, svc_color, bold=True)
                retries_info = ""
                if hc_cfg:
                    retries_info = f" [{failed_count}/{hc_cfg.retries}]"
                lines.append(
                    colorize(f"  ⚠ {name_colored}{retries_info}: {hc_reason}", "yellow")
                )
            elif status == ServiceStatus.ERROR:
                has_issues = True
                svc_color = svc_cfg.color
                name_colored = colorize(name, svc_color, bold=True)
                reason = hc_reason if hc_reason else "已达到最大重启次数"
                lines.append(
                    colorize(
                        f"  ✗ {name_colored}: {reason}（重启次数: {inst.restart_count}/{svc_cfg.max_restarts}）",
                        "bright_red",
                    )
                )

        if has_issues:
            header = colorize("健康检查详情:", "cyan")
            return header + "\n" + "\n".join(lines)
        return ""

    def cmd_logs(self, args: argparse.Namespace) -> int:
        self._load()

        services = args.services if args.services else None

        if args.follow:
            print(colorize("日志模式: 按 Ctrl+C 退出", "bright_black"))
            print()
            try:
                self.log_aggregator.follow(
                    service_names=services,
                    keyword=args.grep,
                )
            except KeyboardInterrupt:
                pass
        else:
            self.log_aggregator.print_history(
                service_names=services,
                keyword=args.grep,
                lines=args.lines,
            )

        return 0

    def cmd_topo(self, args: argparse.Namespace) -> int:
        self._load()

        if self.snapshot_manager.has_snapshot():
            self.snapshot_manager.recover_from_snapshot()

        if args.simple:
            print(self.visualizer.render_simple())
        else:
            print(self.visualizer.render_tree())
            print()
            print(self.visualizer.render_status_table())

        return 0

    def cmd_snapshot(self, args: argparse.Namespace) -> int:
        self._load()

        if args.action == "save":
            if self.snapshot_manager.has_snapshot():
                self.snapshot_manager.recover_from_snapshot()
            for name in self.app_config.get_service_names():
                inst = self.process_manager.get_instance(name)
                if inst:
                    inst.poll()
            path = self.snapshot_manager.save_snapshot()
            print(colorize(f"快照已保存到: {path}", "green"))
            return 0
        elif args.action == "load":
            snapshot = self.snapshot_manager.load_snapshot()
            if snapshot:
                print(colorize("快照信息:", "cyan"))
                import datetime
                ts = datetime.datetime.fromtimestamp(snapshot.timestamp)
                print(f"  时间: {ts}")
                print(f"  服务数量: {len(snapshot.services)}")
                for name, ss in snapshot.services.items():
                    status_color = "green" if ss.pid else "red"
                    pid_str = f"PID:{ss.pid}" if ss.pid else "PID:-"
                    print(f"    {colorize(name, 'cyan')}: {colorize(ss.status, status_color)} ({pid_str})")
            else:
                print(colorize("没有找到快照", "yellow"))
            return 0
        elif args.action == "clear":
            self.snapshot_manager.clear_snapshot()
            print(colorize("快照已清除", "green"))
            return 0
        elif args.action == "recover":
            recovered = self.snapshot_manager.recover_from_snapshot()
            for name, status in recovered.items():
                if status == "recovered":
                    print(f"  {colorize(name, 'green')}: 已恢复")
                else:
                    print(f"  {colorize(name, 'yellow')}: {status}")
            return 0

        return 0

    def cmd_exec(self, args: argparse.Namespace) -> int:
        self._load()

        service_name = args.service
        command = " ".join(args.cmd) if args.cmd else None

        svc = self.app_config.get_service(service_name)
        if not svc:
            print(colorize(f"错误: 服务 '{service_name}' 不存在", "red"))
            return 1

        cwd = os.path.abspath(svc.repo_path)
        env = os.environ.copy()
        for k, v in svc.env.items():
            env[k] = str(v)

        if not command:
            if os.name == "nt":
                command = os.environ.get("COMSPEC", "cmd.exe")
            else:
                command = os.environ.get("SHELL", "/bin/bash")

        print(colorize(f"在 {service_name} 中执行: {command}", "cyan"))
        print(colorize(f"工作目录: {cwd}", "bright_black"))
        print()

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                env=env,
                stdin=None,
                stdout=None,
                stderr=None,
            )
            return result.returncode
        except KeyboardInterrupt:
            return 130

    def cmd_restart(self, args: argparse.Namespace) -> int:
        self._load()

        services = args.services if args.services else self.app_config.get_service_names()

        for name in services:
            if not self.app_config.get_service(name):
                print(colorize(f"错误: 服务 '{name}' 不存在", "red"))
                return 1

        print(colorize("重启服务...", "cyan", bold=True))

        for name in services:
            inst = self.process_manager.get_instance(name)
            svc = self.app_config.get_service(name)
            try:
                self.process_manager.restart_service(name)
                print(f"  {colorize(name, svc.color)}: 已重启")
            except Exception as e:
                print(f"  {colorize(name, 'red')}: 重启失败 - {e}")

        self._save_snapshot_safe()
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mservice",
        description="微服务治理工具 - 管理本地开发环境的多个服务",
    )
    parser.add_argument(
        "-c", "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"配置文件路径 (默认: {DEFAULT_CONFIG_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    start_parser = subparsers.add_parser("start", help="启动服务")
    start_parser.add_argument("services", nargs="*", help="要启动的服务名称 (默认: 全部)")
    start_parser.add_argument("-f", "--follow", action="store_true", help="启动后跟随日志输出")
    start_parser.add_argument("--no-health-check", action="store_true", help="禁用健康检查")
    start_parser.add_argument("--recover", action="store_true", help="从快照恢复状态")

    stop_parser = subparsers.add_parser("stop", help="停止服务")
    stop_parser.add_argument("services", nargs="*", help="要停止的服务名称 (默认: 全部)")
    stop_parser.add_argument("-f", "--force", action="store_true", help="强制停止 (SIGKILL)")
    stop_parser.add_argument("--from-snapshot", action="store_true", help="从快照停止 (用于工具崩溃后)")

    status_parser = subparsers.add_parser("status", help="查看服务状态")
    status_parser.add_argument("--topo", action="store_true", help="以拓扑图形式显示")

    logs_parser = subparsers.add_parser("logs", help="查看服务日志")
    logs_parser.add_argument("services", nargs="*", help="服务名称 (默认: 全部)")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="实时跟随日志")
    logs_parser.add_argument("--grep", help="关键字搜索")
    logs_parser.add_argument("-n", "--lines", type=int, default=100, help="显示最后N行 (默认: 100)")

    topo_parser = subparsers.add_parser("topo", help="显示服务拓扑图")
    topo_parser.add_argument("--simple", action="store_true", help="简单树形视图")

    snapshot_parser = subparsers.add_parser("snapshot", help="快照管理")
    snapshot_parser.add_argument("action", choices=["save", "load", "clear", "recover"], help="快照操作")

    exec_parser = subparsers.add_parser("exec", help="在服务目录中执行命令")
    exec_parser.add_argument("service", help="服务名称")
    exec_parser.add_argument("cmd", nargs=argparse.REMAINDER, help="要执行的命令")

    restart_parser = subparsers.add_parser("restart", help="重启服务")
    restart_parser.add_argument("services", nargs="*", help="要重启的服务名称 (默认: 全部)")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    cli = MServiceCLI(config_path=args.config)

    try:
        if args.command == "start":
            return cli.cmd_start(args)
        elif args.command == "stop":
            return cli.cmd_stop(args)
        elif args.command == "status":
            return cli.cmd_status(args)
        elif args.command == "logs":
            return cli.cmd_logs(args)
        elif args.command == "topo":
            return cli.cmd_topo(args)
        elif args.command == "snapshot":
            return cli.cmd_snapshot(args)
        elif args.command == "exec":
            return cli.cmd_exec(args)
        elif args.command == "restart":
            return cli.cmd_restart(args)
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        print()
        return 130
    except FileNotFoundError as e:
        print(colorize(f"❌ {e}", "bright_red", bold=True))
        return 1
    except CircularDependencyError as e:
        print(colorize(f"❌ {e.message}", "bright_red", bold=True))
        if e.details:
            for d in e.details:
                print(colorize(f"   🔴 {d}", "red"))
        print()
        print(colorize("请修复循环依赖后重新运行", "yellow"))
        return 1
    except ConfigValidationError as e:
        print(colorize(f"❌ {e.message}", "bright_red", bold=True))
        if e.details:
            for d in e.details:
                print(colorize(f"   • {d}", "red"))
        print()
        print(colorize("请修复配置文件后重新运行", "yellow"))
        return 1
    except Exception as e:
        print(colorize(f"错误: {e}", "red"))
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
