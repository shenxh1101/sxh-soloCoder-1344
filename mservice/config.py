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
    for svc_raw in raw.get("services", []):
        hc_raw = svc_raw.get("health_check")
        hc = None
        if hc_raw:
            hc = HealthCheckConfig(
                type=hc_raw.get("type", "tcp"),
                port=hc_raw.get("port"),
                path=hc_raw.get("path", "/"),
                timeout=hc_raw.get("timeout", 5),
                interval=hc_raw.get("interval", 5),
                retries=hc_raw.get("retries", 3),
            )

        ports = []
        for p in svc_raw.get("ports", []):
            if isinstance(p, dict):
                ports.append(PortMapping(host=p["host"], container=p["container"]))
            elif isinstance(p, str) and ":" in p:
                h, c = p.split(":")
                ports.append(PortMapping(host=int(h), container=int(c)))

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

    return AppConfig(services=services, global_config=global_cfg)
