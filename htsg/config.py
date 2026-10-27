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
    kind: Literal["modbus", "opcua"]
    enabled: bool = False
    host: str = ""
    port: int = Field(502, ge=1, le=65535)
    endpoint: str = ""
    interval: Literal[1] = 1
    username_env: str = ""
    password_env: str = ""
    security: str = ""


class Point(StrictModel):
    id: str
    source: str
    unit: str
    low: float | None = None
    high: float | None = None
    hysteresis_low: float | None = Field(None, ge=0)
    hysteresis_high: float | None = Field(None, ge=0)
    extreme_low: float | None = None
    extreme_high: float | None = None
    inclusive: bool = False
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

    @model_validator(mode="after")
    def band(self):
        check_fault_band(self)
        return self


class Limits(StrictModel):
    ingress: int = Field(4096, ge=1, le=100000)  # input batches waiting for the owner
    commit_queue: int = Field(400, ge=1, le=100000)  # commit groups waiting for the one archive writer


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
    ui_poll_interval: float = Field(1, gt=0)  # 修复轮05: the page reads the whole current state, nothing is pushed
    ui_poll_timeout: float = Field(5, gt=0)  # one reading, from the request to the whole answer read and checked
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
        if self.ui_stall_notice <= 2 * self.ui_poll_interval:  # a reading may wait longer than the notice: no relation there
            found.append("UI stall notice must exceed two poll intervals")
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
    limits: Limits = Limits()
    timing: Timing = Timing()
    storage: Storage = Storage()
    project: Project = Project()
    simulation: Simulation = Simulation()
    ui: UI = UI()
    models: tuple[ThermalModel, ...] = ()

    @model_validator(mode="after")
    def references(self):
        if self.dev_admin_login and self.mode != "development":
            raise ValueError("dev_admin_login is only allowed in development mode")
        if len(self.points) > 256 or len(self.derived) > 256 or len(self.sources) > 64:
            raise ValueError("configuration exceeds supported point/derived/source budget")

        def unique(items):
            ids = [x.id for x in items]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate identifiers")

        unique(self.sources)
        unique((*self.points, *self.derived))
        unique(self.models)
        models = {m.id for m in self.models}
        sources = {x.id: x for x in self.sources}
        points = {x.id: x for x in self.points}
        known = set(points)
        units = {x.id: x.unit for x in self.points}
        for point in self.points:
            if point.source not in sources:
                raise ValueError(f"{point.id}: unknown source")
            source = sources[point.source]
            if source.enabled and source.kind == "modbus" and (not source.host or point.address is None):
                raise ValueError("enabled Modbus needs host and register mapping")
            if source.enabled and source.kind == "opcua" and (not source.endpoint or not point.node_id):
                raise ValueError("enabled OPC UA needs endpoint and node mapping")
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
            if derived.operation == "change" and len(derived.inputs) != 1:
                raise ValueError("change needs one input")
            if derived.operation in ("change", "maximum") and any(
                units.get(x) != derived.unit for x in derived.inputs
            ):
                raise ValueError("change/maximum requires matching input and output units")
            if (
                derived.operation in ("sum", "mean", "difference", "maximum")
                and len({units[x] for x in derived.inputs}) > 1
            ):
                raise ValueError("additive calculation requires consistent input units")
            units[derived.id] = derived.unit
            known.add(derived.id)
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
