"""动态站点持久化。

用户在界面/API 中配置的站点全量保存到 data/stations.json；
重启时若该文件存在则优先于 config.yaml 的站点列表（用户配置为准），
不存在则使用 config.yaml 作为初始种子。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import StationConfig


class StationStore:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "stations.json"

    def load(self) -> list[StationConfig] | None:
        """读取用户配置的站点列表；文件不存在返回 None。"""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("根节点必须是对象")
            items = data.get("stations")
            if not isinstance(items, list):
                raise ValueError("stations 必须是列表")
            stations = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("每个站点必须是对象")
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("站点名称不能为空")
                if not isinstance(item.get("base_url"), str) or not item["base_url"].strip():
                    raise ValueError(f"站点 [{name}] 缺少 base_url")
                if not isinstance(item.get("api_key"), str) or not item["api_key"].strip():
                    raise ValueError(f"站点 [{name}] 缺少 api_key")
                models = item.get("models", [])
                if not isinstance(models, list) or any(
                        not isinstance(model, str) or not model.strip()
                        for model in models):
                    raise ValueError(f"站点 [{name}] models 必须是字符串列表")
                concurrency = item.get("max_concurrency", 20)
                if isinstance(concurrency, bool) or not isinstance(concurrency, int) \
                        or concurrency < 1:
                    raise ValueError(f"站点 [{name}] max_concurrency 必须大于 0")
                stations.append(StationConfig.from_dict(name.strip(), item))
            names = [station.name.casefold() for station in stations]
            if len(set(names)) != len(names):
                raise ValueError("站点名称必须唯一")
            return stations
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as e:
            print(f"[stations] 加载失败（忽略，使用 config.yaml）: {e}")
            return None

    def save(self, stations: list[StationConfig]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"stations": [dataclass_to_dict(s) for s in stations]}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            print(f"[stations] 保存失败: {e}")
            raise


def dataclass_to_dict(cfg: StationConfig) -> dict:
    """StationConfig → dict（含默认值，便于用户编辑）。"""
    d = {
        "name": cfg.name,
        "base_url": cfg.base_url,
        "api_key": cfg.api_key,
        "models": cfg.models,
        "model_routing": cfg.model_routing,
        "route_models": cfg.route_models,
        "provider": cfg.provider,
        "opencode_plan": cfg.opencode_plan,
        "priority": cfg.priority,
        "priority_auto": cfg.priority_auto,
        "weight": cfg.weight,
        "timeout": cfg.timeout,
        "max_concurrency": cfg.max_concurrency,
        "probe_models": cfg.probe_models,
        "probe_path": cfg.probe_path,
        "probe_interval": cfg.probe_interval,
        "probe_chat": cfg.probe_chat,
        "probe_chat_path": cfg.probe_chat_path,
        "probe_chat_model": cfg.probe_chat_model,
        "probe_balance": cfg.probe_balance,
        "billing": cfg.billing,
        "headers": cfg.headers,
        "wire_api": cfg.wire_api,
        "inject": cfg.inject,
        "enabled": cfg.enabled,
    }
    return d
