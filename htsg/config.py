from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .research_config import UI, Project, Simulation, validate_research
from .thermal import ThermalModel


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def check_fault_band(item):
    """普通越限阈值与回差：回差只配在有阈值的一侧，且复归区间不能为空。"""
    if item.low is not None and item.high is not None and item.low >= item.high:
        raise ValueError("low must be less than high")
    if item.hysteresis_low is not None and item.low is None:
        raise ValueError("hysteresis_low needs low")
    if item.hysteresis_high is not None and item.high is None:
        raise ValueError("hysteresis_high needs high")
    if item.low is not None and item.high is not None:
        if item.high - (item.hysteresis_high or 0) <= item.low + (item.hysteresis_low or 0):
            raise ValueError("hysteresis leaves no reset band between low and high")
    # Valid values never leave the extreme band, so a reset line outside it could never be reached.
    if item.high is not None and item.extreme_low is not None:
        if item.high - (item.hysteresis_high or 0) <= item.extreme_low:
            raise ValueError("high-side reset line lies below extreme_low and is unreachable")
    if item.low is not None and item.extreme_high is not None:
        if item.low + (item.hysteresis_low or 0) >= item.extreme_high:
            raise ValueError("low-side reset line lies above extreme_high and is unreachable")


def fault_rule(item):
    """普通故障规则（与极值界限无关）；没有任何阈值则为 None。"""
    if item.low is None and item.high is None:
        return None
    return {
        "low": item.low,
        "high": item.high,
        "hysteresis_low": item.hysteresis_low or 0.0,
        "hysteresis_high": item.hysteresis_high or 0.0,
    }


class Source(StrictModel):
    id: str
    kind: Literal["simulator", "modbus", "opcua"]
    enabled: bool = False
    host: str = ""
    port: int = Field(502, ge=1, le=65535)
    endpoint: str = ""
    interval: Literal[1] = 1
    timeout: float = Field(3, gt=0, le=60)
    username_env: str = ""
    password_env: str = ""
    security: str = ""


class Point(StrictModel):
    id: str
    source: str
    unit: str
    aggregation: Literal["Direct"] = "Direct"
    low: float | None = None
    high: float | None = None
    hysteresis_low: float | None = Field(None, ge=0)
    hysteresis_high: float | None = Field(None, ge=0)
    extreme_low: float | None = None
    extreme_high: float | None = None
    inclusive: bool = False
    stale_after: float = Field(5, gt=0)
    address: int | None = Field(None, ge=0, le=65535)
    device_id: int = Field(1, ge=0, le=255)
    register_kind: Literal["holding", "input"] = "holding"
    data_type: Literal["uint16", "int16", "uint32", "int32", "float32", "float64"] = "float32"
    byte_order: Literal["big", "little"] = "big"
    word_order: Literal["big", "little"] = "big"
    node_id: str = ""
    scale: float = 1
    offset: float = 0
    invalid_values: tuple[float, ...] = ()
    simulator_base: float = 10
    simulator_amplitude: float = 2

    @model_validator(mode="after")
    def limits(self):
        check_fault_band(self)
        if (
            self.extreme_low is not None
            and self.extreme_high is not None
            and self.extreme_low >= self.extreme_high
        ):
            raise ValueError("extreme_low must be less than extreme_high")
        return self


class Derived(StrictModel):
    id: str
    inputs: tuple[str, ...]
    operation: Literal[
        "sum",
        "difference",
        "ratio",
        "mean",
        "derivative",
        "change",
        "maximum",
        "unconfigured",
        "bridge_strain",
        "thermal_stress",
    ]
    model_id: str | None = None
    unit: str
    low: float | None = None
    high: float | None = None
    hysteresis_low: float | None = Field(None, ge=0)
    hysteresis_high: float | None = Field(None, ge=0)
    extreme_low: float | None = None
    extreme_high: float | None = None
    scale: float = 1
    offset: float = 0
    max_age: float = Field(2, ge=0)

    @model_validator(mode="after")
    def band(self):
        check_fault_band(self)
        return self


class Profile(StrictModel):
    id: str
    trigger_points: tuple[str, ...]
    source_points: tuple[str, ...]
    derived_points: tuple[str, ...] = ()
    pre: float = Field(600, gt=0, le=3600)
    recovery: float = Field(60, gt=0, le=3600)
    post: float = Field(1200, ge=0, le=86400)


class Limits(StrictModel):
    capture_budget_bytes: int = Field(64 * 1024**2, ge=1024, le=512 * 1024**2)
    write_batch_records: int = Field(128, ge=1, le=1024)
    maintenance_interval: Literal[1] = 1
    maintenance_budget: float = Field(0.2, ge=0.01, le=3)
    cleanup_batch: int = Field(1024, ge=1, le=10000)
    ingress: int = Field(4096, ge=1, le=100000)
    ordinary_queue: int = Field(4096, ge=1, le=10000)
    event_queue: int = Field(2048, ge=1, le=100000)
    ring_samples: int = Field(10000, ge=1, le=1000000)
    samples_per_window: int = Field(10000, ge=1, le=1000000)
    window_wait: Literal[0] = 0
    clock_jump: float = Field(2, gt=0)
    historical_age: float = Field(3600, gt=0)
    max_capture: float = Field(86400, gt=0)
    unavailable_timeout: float = Field(600, gt=0)
    ws_queue: int = Field(64, ge=2, le=1000)
    max_clients: int = Field(16, ge=1, le=128)
    calculation_timeout: float = Field(0.2, gt=0, le=5)


class Timing(StrictModel):
    """终版 §11 参数表。这里是请求值，不是生效值：服务端修订订阅参数后必须用
    `check_negotiated` 对最终值重新核对。"""

    acquisition_period: Literal[1] = 1
    modbus_connect_timeout: float = Field(0.8, gt=0, lt=1)
    modbus_request_timeout: float = Field(0.8, gt=0, lt=1)
    modbus_retries: Literal[0] = 0
    modbus_reconnect_timeouts: int = Field(3, ge=1)
    modbus_alarm_seconds: float = Field(10, gt=0)
    ua_publish_interval: float = Field(1, gt=0)
    ua_sampling_interval: float = Field(1, gt=0)
    ua_queue_size: int = Field(10, ge=1)
    ua_keepalive_count: int = Field(2, ge=1)
    ua_lifetime_count: int = Field(60, ge=1)
    ua_session_timeout: float = Field(60, gt=0)
    ua_request_timeout: float = Field(5, gt=0)
    ua_data_validity: float = Field(3, gt=0)
    ua_alarm_seconds: float = Field(6, gt=0)
    # 终版 §11 要求"静默间隔加调度/传输裕量小于数据有效期"，但未给裕量数值；0.5 秒为实现取值。
    ua_silence_margin: float = Field(0.5, ge=0)
    backoff_initial: float = Field(1, gt=0)
    backoff_max: float = Field(30, gt=0)
    backoff_stable_seconds: float = Field(60, gt=0)
    resync_persistent_after: int = Field(5, ge=1)
    clock_jump: float = Field(2, gt=0)
    db_connect_timeout: float = Field(3, gt=0)
    db_statement_timeout: float = Field(3, gt=0)
    db_lock_timeout: float = Field(1, gt=0)
    watermark_interval: float = Field(10, gt=0)
    stats_interval: float = Field(60, gt=0)
    status_interval: float = Field(1, gt=0)
    push_coalesce: float = Field(0.1, gt=0)
    push_heartbeat: float = Field(1, gt=0)
    client_send_timeout: float = Field(5, gt=0)
    ui_stall_notice: float = Field(3, gt=0)
    fault_confirm_seconds: float = Field(5, gt=0)
    fault_buffer_limit: int = Field(1000, ge=1)

    def problems(self, publish_interval, keepalive_count, lifetime_count):
        """违反 §11 约束的条目；入参为请求值或服务端修订后的最终值。"""
        silence = publish_interval * keepalive_count
        found = []
        if silence + self.ua_silence_margin >= self.ua_data_validity:
            found.append("keepalive silence plus margin must be below the data validity")
        if self.ua_alarm_seconds <= self.ua_data_validity:
            found.append("alarm threshold must exceed the data validity")
        if self.ua_alarm_seconds < 3 * silence:
            found.append("alarm threshold must be at least 3x the keepalive silence")
        if lifetime_count < 3 * keepalive_count:
            found.append("lifetime count must be at least 3x the keepalive count")
        return found

    def check_negotiated(self, publish_interval, keepalive_count, lifetime_count):
        return self.problems(publish_interval, keepalive_count, lifetime_count)

    @staticmethod
    def queue_capability(revised_size):
        """队列被修订为 1 时照常运行，但中间覆盖不可检测。"""
        return "OVERWRITE_UNDETECTABLE" if revised_size <= 1 else "OVERFLOW_DETECTABLE"

    @model_validator(mode="after")
    def relations(self):
        found = self.problems(self.ua_publish_interval, self.ua_keepalive_count, self.ua_lifetime_count)
        if self.ui_stall_notice <= 2 * self.push_heartbeat:
            found.append("UI stall notice must exceed two heartbeat intervals")
        if self.backoff_initial > self.backoff_max:
            found.append("backoff_initial must not exceed backoff_max")
        if found:
            raise ValueError("; ".join(found))
        return self


class Storage(StrictModel):
    primary_path: str = "D:/HTSG-dev/primary"
    emergency_path: str = "D:/HTSG-dev/emergency"
    external_enabled: bool = False
    external_path: str = ""
    external_start: float = Field(0.95, gt=0, lt=1)
    cleanup_enabled: bool = False
    cleanup_start: float = Field(0.8, gt=0, lt=1)
    cleanup_stop: float = Field(0.7, gt=0, lt=1)
    emergency_start: float = Field(0.95, gt=0, lt=1)
    emergency_reserve_bytes: int = Field(100 * 1024**3, ge=0)
    batch_budget_bytes: int = Field(8 * 1024**2, gt=0)
    development_budget_bytes: int = Field(10 * 1024**3, ge=1024, le=10 * 1024**3)

    @model_validator(mode="after")
    def watermarks(self):
        if not self.cleanup_stop < self.cleanup_start < self.emergency_start:
            raise ValueError("require stop < cleanup_start < emergency_start")
        if self.external_enabled and not self.external_path.strip():
            raise ValueError("enabled external storage needs external_path")
        if self.external_start > self.emergency_start:
            raise ValueError("external_start must not exceed emergency_start")
        return self


class Settings(StrictModel):
    mode: Literal["development", "production"] = "development"
    dev_admin_login: bool = False
    time_basis: Literal["received", "source"] = "received"
    sources: tuple[Source, ...] = ()
    points: tuple[Point, ...] = ()
    derived: tuple[Derived, ...] = ()
    profiles: tuple[Profile, ...] = ()
    limits: Limits = Limits()
    timing: Timing = Timing()
    storage: Storage = Storage()
    allowed_origins: tuple[str, ...] = ()
    project: Project = Project()
    simulation: Simulation = Simulation()
    ui: UI = UI()
    models: tuple[ThermalModel, ...] = ()

    @model_validator(mode="after")
    def references(self):
        if self.dev_admin_login and self.mode != "development":
            raise ValueError("dev_admin_login is only allowed in development mode")
        if (
            len(self.points) > 256
            or len(self.derived) > 256
            or len(self.profiles) > 64
            or len(self.sources) > 64
        ):
            raise ValueError("configuration exceeds supported point/derived/profile/source budget")

        def unique(items):
            ids = [x.id for x in items]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate identifiers")

        unique(self.sources)
        unique((*self.points, *self.derived))
        unique(self.profiles)
        unique(self.models)
        models = {m.id for m in self.models}
        sources = {x.id: x for x in self.sources}
        points = {x.id: x for x in self.points}
        known = set(points)
        deps = {x.id: {x.id} for x in self.points}
        units = {x.id: x.unit for x in self.points}
        for point in self.points:
            if point.source not in sources:
                raise ValueError(f"{point.id}: unknown source")
            source = sources[point.source]
            if source.enabled and source.kind == "modbus" and (not source.host or point.address is None):
                raise ValueError("enabled Modbus needs host and register mapping")
            if source.enabled and source.kind == "opcua" and (not source.endpoint or not point.node_id):
                raise ValueError("enabled OPC UA needs endpoint and node mapping")
            if source.kind == "simulator" and source.enabled and self.mode == "production":
                raise ValueError("simulator forbidden in production")
        if sum(sources[x.source].kind == "modbus" for x in self.points) > 8:
            raise ValueError("Modbus Bridge channel capacity is 8")
        if sum(sources[x.source].kind == "opcua" for x in self.points) > 200:
            raise ValueError("OPC UA channel capacity is 200")
        for derived in self.derived:
            if (
                derived.extreme_low is not None
                and derived.extreme_high is not None
                and derived.extreme_low >= derived.extreme_high
            ):
                raise ValueError("extreme_low must be less than extreme_high")
            if derived.operation == "bridge_strain":
                if (
                    len(derived.inputs) != 1
                    or units.get(derived.inputs[0]) != "mV/V"
                    or derived.unit != "με"
                    or derived.model_id is not None
                    or derived.scale != 1
                    or derived.offset != 0
                ):
                    raise ValueError("bridge_strain needs one mV/V input, με output and identity scaling")
            elif derived.operation == "thermal_stress":
                if (
                    derived.model_id not in models
                    or len(derived.inputs) != 2
                    or any(units.get(x) not in ("°C", "degC", "C") for x in derived.inputs)
                    or len({units.get(x) for x in derived.inputs}) != 1
                    or derived.unit != "MPa"
                    or derived.scale != 1
                    or derived.offset != 0
                ):
                    raise ValueError(
                        "thermal_stress needs a model, two Celsius inputs, MPa and identity scaling"
                    )
            elif derived.model_id is not None:
                raise ValueError("model_id is only supported by thermal_stress")
            if not set(derived.inputs) <= known or (
                not derived.inputs and derived.operation != "unconfigured"
            ):
                raise ValueError("derived inputs must be topologically ordered and exist")
            if derived.operation in ("difference", "ratio") and len(derived.inputs) != 2:
                raise ValueError("operation needs two inputs")
            if derived.operation in ("derivative", "change") and len(derived.inputs) != 1:
                raise ValueError("derivative/change needs one input")
            if derived.operation in ("change", "maximum") and any(
                units.get(x) != derived.unit for x in derived.inputs
            ):
                raise ValueError("change/maximum requires matching input and output units")
            if (
                derived.operation in ("sum", "mean", "difference", "maximum")
                and len({units[x] for x in derived.inputs}) > 1
            ):
                raise ValueError("additive calculation requires consistent input units")
            deps[derived.id] = set().union(*(deps[x] for x in derived.inputs))
            units[derived.id] = derived.unit
            known.add(derived.id)
        for profile in self.profiles:
            if not profile.trigger_points or not set(profile.trigger_points) <= set(profile.source_points):
                raise ValueError("all triggers must be captured")
            if not set(profile.source_points) <= set(points) or not set(profile.derived_points) <= set(
                deps
            ) - set(points):
                raise ValueError("unknown profile dependency")
            if any(not deps[x] <= set(profile.source_points) for x in profile.derived_points):
                raise ValueError("profile must capture every derived source dependency")
            if any(points[x].low is None and points[x].high is None for x in profile.trigger_points):
                raise ValueError("trigger point needs a threshold")
            if self.mode == "production" and (profile.pre, profile.recovery, profile.post) != (600, 60, 1200):
                raise ValueError("production capture durations are fixed at 600/60/1200 seconds")
        if self.mode == "development":
            paths = [self.storage.primary_path, self.storage.emergency_path]
            if self.storage.external_enabled:
                paths.append(self.storage.external_path)
            for path in paths:
                if not path.replace("\\", "/").upper().startswith("D:/"):
                    raise ValueError("development databases must be on D:")
        return validate_research(self)

    @property
    def config_id(self):
        # Canonical form: a stored JSONB snapshot does not keep key order, and must hash identically.
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def load_settings(path: str | Path) -> Settings:
    return Settings.model_validate(json.loads(Path(path).read_text(encoding="utf-8-sig")))
