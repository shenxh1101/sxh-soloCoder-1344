import os
import yaml
from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class HealthCheckConfig:
    type: str = "tcp"
    port: Optional[int] = None
    path: str = "/"
    timeout: int = 5
    interval: int = 5
    retries: int = 3


@dataclass
class PortMapping:
    host: int
    container: int


@dataclass
class ServiceConfig:
    name: str
    repo_path: str
    command: str
    dependencies: List[str] = field(default_factory=list)
    health_check: Optional[HealthCheckConfig] = None
    env: Dict[str, str] = field(default_factory=dict)
    ports: List[PortMapping] = field(default_factory=list)
    color: str = "cyan"
    max_restarts: int = 3


@dataclass
class GlobalConfig:
    log_dir: str = "./logs"
    snapshot_file: str = "./.mservice_snapshot.json"
    health_check_interval: int = 5


@dataclass
class AppConfig:
    services: List[ServiceConfig]
    global_config: GlobalConfig = field(default_factory=GlobalConfig)

    def get_service(self, name: str) -> Optional[ServiceConfig]:
        for svc in self.services:
            if svc.name == name:
                return svc
        return None

    def get_service_names(self) -> List[str]:
        return [svc.name for svc in self.services]


class ConfigValidationError(Exception):
    def __init__(self, message: str, details: list = None):
        super().__init__(message)
        self.details = details or []
        self.message = message

    def __str__(self):
        if self.details:
            detail_lines = "\n".join(f"  - {d}" for d in self.details)
            return f"{self.message}\n{detail_lines}"
        return self.message


class CircularDependencyError(ConfigValidationError):
    pass


def load_config(config_path: str) -> AppConfig:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    global_cfg = GlobalConfig()
    if "global" in raw and raw["global"]:
        g = raw["global"]
        global_cfg.log_dir = g.get("log_dir", global_cfg.log_dir)
        global_cfg.snapshot_file = g.get("snapshot_file", global_cfg.snapshot_file)
        global_cfg.health_check_interval = g.get(
            "health_check_interval", global_cfg.health_check_interval
        )

    services = []
    for i, svc_raw in enumerate(raw.get("services", [])):
        if "name" not in svc_raw:
            raise ConfigValidationError(
                f"配置错误: 第 {i + 1} 个服务缺少 'name' 字段",
            )
        if "repo_path" not in svc_raw:
            raise ConfigValidationError(
                f"配置错误: 服务 '{svc_raw.get('name', f'<第{i+1}个>')}' 缺少 'repo_path' 字段",
            )
        if "command" not in svc_raw:
            raise ConfigValidationError(
                f"配置错误: 服务 '{svc_raw.get('name')}' 缺少 'command' 字段",
            )

        hc_raw = svc_raw.get("health_check")
        hc = None
        if hc_raw:
            hc_type = hc_raw.get("type", "tcp")
            if hc_type not in ("http", "tcp"):
                raise ConfigValidationError(
                    f"配置错误: 服务 '{svc_raw['name']}' 的 health_check.type 必须是 'http' 或 'tcp'，实际是 '{hc_type}'",
                )
            if hc_type in ("http", "tcp") and hc_raw.get("port") is None:
                if not svc_raw.get("ports"):
                    raise ConfigValidationError(
                        f"配置错误: 服务 '{svc_raw['name']}' 配置了健康检查 ({hc_type})，但未指定 port，也没有配置 ports 列表",
                    )
            hc = HealthCheckConfig(
                type=hc_type,
                port=hc_raw.get("port"),
                path=hc_raw.get("path", "/"),
                timeout=hc_raw.get("timeout", 5),
                interval=hc_raw.get("interval", 5),
                retries=hc_raw.get("retries", 3),
            )

        ports = []
        for j, p in enumerate(svc_raw.get("ports", [])):
            try:
                if isinstance(p, dict):
                    if "host" not in p or "container" not in p:
                        raise ConfigValidationError(
                            f"配置错误: 服务 '{svc_raw['name']}' 的第 {j + 1} 个 ports 配置缺少 host/container 字段",
                        )
                    ports.append(PortMapping(host=p["host"], container=p["container"]))
                elif isinstance(p, str) and ":" in p:
                    h, c = p.split(":")
                    ports.append(PortMapping(host=int(h), container=int(c)))
                else:
                    raise ConfigValidationError(
                        f"配置错误: 服务 '{svc_raw['name']}' 的第 {j + 1} 个 ports 格式无效 (需要 'host:container' 字符串或 dict)",
                    )
            except ValueError:
                raise ConfigValidationError(
                    f"配置错误: 服务 '{svc_raw['name']}' 的第 {j + 1} 个 ports '{p}' 格式无效 (端口号必须是整数)",
                )

        svc = ServiceConfig(
            name=svc_raw["name"],
            repo_path=svc_raw["repo_path"],
            command=svc_raw["command"],
            dependencies=svc_raw.get("depends_on", []),
            health_check=hc,
            env=svc_raw.get("environment", {}),
            ports=ports,
            color=svc_raw.get("color", "cyan"),
            max_restarts=svc_raw.get("max_restarts", 3),
        )
        services.append(svc)

    _validate_dependencies(services)

    return AppConfig(services=services, global_config=global_cfg)


def _validate_dependencies(services: list) -> None:
    service_names = {svc.name for svc in services}
    errors = []

    for svc in services:
        for dep in svc.dependencies:
            if dep not in service_names:
                existing = sorted(service_names)
                suggestion = _find_similar(dep, existing)
                if suggestion:
                    errors.append(
                        f"服务 '{svc.name}' 的依赖 '{dep}' 不存在（是否想写 '{suggestion}'？）"
                    )
                else:
                    errors.append(
                        f"服务 '{svc.name}' 的依赖 '{dep}' 不存在（可用服务: {', '.join(existing)}）"
                    )

    if errors:
        raise ConfigValidationError(
            f"配置错误: 发现 {len(errors)} 个无效的依赖项",
            details=errors,
        )

    _detect_circular_dependencies(services)


def _find_similar(target: str, candidates: list) -> str:
    target_lower = target.lower()
    best_match = ""
    best_score = 0

    for c in candidates:
        c_lower = c.lower()
        if c_lower == target_lower:
            return c

        score = 0
        min_len = min(len(target_lower), len(c_lower))
        for i in range(min_len):
            if target_lower[i] == c_lower[i]:
                score += 1

        if target_lower in c_lower or c_lower in target_lower:
            score += min(len(target_lower), len(c_lower))

        if score > best_score:
            max_len = max(len(target_lower), len(c_lower))
            similarity = score / max_len
            if similarity > 0.5:
                best_score = score
                best_match = c

    return best_match


def _detect_circular_dependencies(services: list) -> None:
    dep_map = {svc.name: list(svc.dependencies) for svc in services}
    visited = set()
    path = []

    def dfs(node: str):
        if node in path:
            cycle_start = path.index(node)
            cycle = path[cycle_start:] + [node]
            raise CircularDependencyError(
                "配置错误: 检测到循环依赖",
                details=[" → ".join(cycle)],
            )
        if node in visited:
            return
        path.append(node)
        for dep in dep_map.get(node, []):
            dfs(dep)
        path.pop()
        visited.add(node)

    for svc in services:
        dfs(svc.name)
