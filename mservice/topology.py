from typing import Dict, List
from collections import deque

from .config import AppConfig
from .process_manager import ProcessManager, ServiceStatus
from .utils import colorize


STATUS_SYMBOLS = {
    ServiceStatus.RUNNING: "●",
    ServiceStatus.STARTING: "◐",
    ServiceStatus.UNHEALTHY: "○",
    ServiceStatus.STOPPED: "○",
    ServiceStatus.EXITED: "×",
    ServiceStatus.ERROR: "✕",
}

STATUS_COLORS = {
    ServiceStatus.RUNNING: "green",
    ServiceStatus.STARTING: "yellow",
    ServiceStatus.UNHEALTHY: "red",
    ServiceStatus.STOPPED: "bright_black",
    ServiceStatus.EXITED: "red",
    ServiceStatus.ERROR: "bright_red",
}

STATUS_LABELS = {
    ServiceStatus.RUNNING: "运行中",
    ServiceStatus.STARTING: "启动中",
    ServiceStatus.UNHEALTHY: "不健康",
    ServiceStatus.STOPPED: "已停止",
    ServiceStatus.EXITED: "已退出",
    ServiceStatus.ERROR: "错误",
}


class TopologyVisualizer:
    def __init__(self, app_config: AppConfig, process_manager: ProcessManager):
        self.app_config = app_config
        self.process_manager = process_manager

    def _get_status(self, name: str) -> ServiceStatus:
        try:
            return self.process_manager.get_status(name)
        except Exception:
            return ServiceStatus.STOPPED

    def _format_node(self, name: str, max_width: int) -> str:
        status = self._get_status(name)
        symbol = STATUS_SYMBOLS.get(status, "○")
        color = STATUS_COLORS.get(status, "white")
        label = STATUS_LABELS.get(status, "unknown")

        svc = self.app_config.get_service(name)
        svc_color = svc.color if svc else "white"

        name_colored = colorize(name, svc_color, bold=True)
        symbol_colored = colorize(symbol, color)
        status_colored = colorize(label, color)

        pad = max_width - len(name)
        box_top = f"┌{'─' * (max_width + 4)}┐"
        box_mid = f"│ {symbol_colored} {name_colored}{' ' * pad} │"
        box_bot = f"│   {status_colored}{' ' * (max_width + 4 - len(label) - 3)}│"
        box_end = f"└{'─' * (max_width + 4)}┘"

        return [box_top, box_mid, box_bot, box_end]

    def render_tree(self) -> str:
        services = self.app_config.services
        if not services:
            return "(no services configured)"

        max_name_len = max(len(s.name) for s in services)

        order = self.process_manager.topological_sort()
        lines = []

        dependency_map: Dict[str, List[str]] = {}
        reverse_deps: Dict[str, List[str]] = {}
        for svc in services:
            dependency_map[svc.name] = list(svc.dependencies)
            for dep in svc.dependencies:
                if dep not in reverse_deps:
                    reverse_deps[dep] = []
                reverse_deps[dep].append(svc.name)

        levels: Dict[str, int] = {}
        for name in order:
            deps = dependency_map.get(name, [])
            if not deps:
                levels[name] = 0
            else:
                levels[name] = max(levels.get(d, 0) for d in deps) + 1

        max_level = max(levels.values()) if levels else 0
        level_groups: Dict[int, List[str]] = {}
        for name, lvl in levels.items():
            if lvl not in level_groups:
                level_groups[lvl] = []
            level_groups[lvl].append(name)

        box_width = max_name_len + 4
        col_width = box_width + 6

        result_lines = []

        level_lists = [level_groups.get(i, []) for i in range(max_level + 1)]
        max_per_level = max(len(grp) for grp in level_lists) if level_lists else 0

        node_lines_per = 4

        grid = [[" " * (col_width * (max_level + 1)) for _ in range(max_per_level * node_lines_per)]]

        result = []
        for row in range(max_per_level * node_lines_per):
            line = ""
            for level in range(max_level + 1):
                services_in_level = level_groups.get(level, [])
                node_index = row // node_lines_per
                node_row = row % node_lines_per

                if node_index < len(services_in_level):
                    name = services_in_level[node_index]
                    node_box = self._format_node(name, max_name_len)
                    cell_content = node_box[node_row] if node_row < len(node_box) else ""
                else:
                    cell_content = " " * (box_width + 2)

                has_next = level < max_level
                next_services = level_groups.get(level + 1, [])
                next_node_index = row // node_lines_per
                next_node_row = row % node_lines_per

                if has_next and node_row == 1 and node_index < len(services_in_level):
                    name = services_in_level[node_index]
                    has_dep_to_next = any(
                        s in reverse_deps.get(name, []) for s in next_services
                    )
                    if has_dep_to_next:
                        cell_content += "───▶ "
                    else:
                        cell_content += "     "
                elif has_next:
                    cell_content += "     "

                line += cell_content
            result.append(line)

        result_str = "\n".join(result)

        legend_lines = [
            "",
            "状态图例:",
        ]
        for status in [
            ServiceStatus.RUNNING,
            ServiceStatus.STARTING,
            ServiceStatus.UNHEALTHY,
            ServiceStatus.STOPPED,
            ServiceStatus.EXITED,
        ]:
            symbol = colorize(STATUS_SYMBOLS[status], STATUS_COLORS[status])
            label = STATUS_LABELS[status]
            legend_lines.append(f"  {symbol} {label}")

        return result_str + "\n" + "\n".join(legend_lines)

    def render_simple(self) -> str:
        lines = []
        lines.append(colorize("服务依赖拓扑图", "cyan", bold=True))
        lines.append("=" * 50)
        lines.append("")

        order = self.process_manager.topological_sort()
        visited = set()

        def print_tree(name: str, prefix: str = "", is_last: bool = True, level: int = 0):
            if name in visited:
                return
            visited.add(name)

            status = self._get_status(name)
            symbol = STATUS_SYMBOLS.get(status, "○")
            color = STATUS_COLORS.get(status, "white")

            svc = self.app_config.get_service(name)
            svc_color = svc.color if svc else "white"

            if level == 0:
                connector = ""
            else:
                connector = "└── " if is_last else "├── "

            name_colored = colorize(name, svc_color, bold=True)
            symbol_colored = colorize(symbol, color)
            status_label = colorize(STATUS_LABELS.get(status, ""), color)

            line = f"{prefix}{connector}{symbol_colored} {name_colored}  [{status_label}]"
            lines.append(line)

            if level == 0:
                child_prefix = ""
            else:
                child_prefix = prefix + ("    " if is_last else "│   ")

            children = []
            for svc_cfg in self.app_config.services:
                if name in svc_cfg.dependencies:
                    children.append(svc_cfg.name)

            children.sort()
            for i, child in enumerate(children):
                is_last_child = i == len(children) - 1
                print_tree(child, child_prefix, is_last_child, level + 1)

        roots = []
        for svc in self.app_config.services:
            if not svc.dependencies:
                roots.append(svc.name)

        if not roots:
            roots = [self.app_config.services[0].name]

        for i, root in enumerate(roots):
            print_tree(root, "", True, 0)

        lines.append("")
        lines.append("依赖方向: 被依赖 → 依赖者")
        lines.append("")

        legend = []
        legend.append("状态图例:")
        for status in [
            ServiceStatus.RUNNING,
            ServiceStatus.STARTING,
            ServiceStatus.UNHEALTHY,
            ServiceStatus.STOPPED,
            ServiceStatus.EXITED,
            ServiceStatus.ERROR,
        ]:
            sym = colorize(STATUS_SYMBOLS[status], STATUS_COLORS[status])
            lab = STATUS_LABELS[status]
            legend.append(f"  {sym} {lab}")

        lines.append(" | ".join(legend[:3]))
        lines.append(" | ".join(legend[3:]))

        return "\n".join(lines)

    def render_status_table(self) -> str:
        lines = []
        lines.append(colorize("服务状态一览", "cyan", bold=True))
        lines.append("-" * 60)

        for svc in self.app_config.services:
            status = self._get_status(svc.name)
            symbol = colorize(STATUS_SYMBOLS.get(status, "○"), STATUS_COLORS.get(status, "white"))
            status_label = colorize(STATUS_LABELS.get(status, "unknown"), STATUS_COLORS.get(status, "white"))
            name_colored = colorize(svc.name, svc.color, bold=True)

            inst = self.process_manager.get_instance(svc.name)
            pid_str = f"PID:{inst.pid}" if inst and inst.pid else "PID:-"
            restart_str = f"重启:{inst.restart_count}" if inst else "重启:0"

            line = f"  {symbol} {name_colored:<20s}  {status_label:<10s}  {pid_str:<10s}  {restart_str}"
            lines.append(line)

        lines.append("-" * 60)
        return "\n".join(lines)
