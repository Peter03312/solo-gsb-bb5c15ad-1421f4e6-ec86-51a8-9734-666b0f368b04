"""请求体模型与校验。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import FiniteFloat


def _require_tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("必须携带时区偏移（如 2026-10-01T08:00:00+08:00 或 ...Z）")
    return v


class StationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rise_threshold: FiniteFloat = Field(description="上升阈值")
    clear_threshold: FiniteFloat = Field(description="解除阈值，必须低于上升阈值")

    @model_validator(mode="after")
    def _check_thresholds(self) -> "StationCreate":
        if not self.rise_threshold > self.clear_threshold:
            raise ValueError("上升阈值必须严格大于解除阈值")
        return self


class DeviceCreate(BaseModel):
    device_no: str = Field(min_length=1, max_length=100, description="设备号（全局唯一）")


class DeviceTakeoverCreate(BaseModel):
    """汛期一站多设备轮值：登记本站设备的接管记录。

    自生效时刻（含同刻）起，仅该接管记录指定设备的有效读数进入本站裁定，
    缺测不借用其他设备；同站同一生效瞬间只允许一条。
    """

    device_no: str = Field(min_length=1, max_length=100, description="接管设备号（须隶属本站）")
    effective_at: datetime = Field(description="带时区的生效时间，如 2026-10-02T08:00:00+08:00")
    reason: str = Field(min_length=1, max_length=500, description="接管原因（必填）")

    _tz = field_validator("effective_at")(_require_tz_aware)

    @field_validator("device_no", "reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("不得为空白")
        return v


class ReadingIngest(BaseModel):
    device_no: str = Field(min_length=1, max_length=100)
    seq: int = Field(ge=0, description="设备内递增序号")
    observed_at: datetime = Field(description="带时区的观测时间")
    water_level: FiniteFloat = Field(description="水位")

    _tz = field_validator("observed_at")(_require_tz_aware)


class VoidRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, description="作废原因（必填）")
    auditor: Optional[str] = Field(default=None, max_length=100, description="审核员")


class CorrectRequest(BaseModel):
    observed_at: Optional[datetime] = None
    water_level: Optional[FiniteFloat] = None
    reason: str = Field(min_length=1, max_length=500, description="更正原因（必填）")
    auditor: Optional[str] = Field(default=None, max_length=100)

    _tz = field_validator("observed_at")(_require_tz_aware)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "CorrectRequest":
        if self.observed_at is None and self.water_level is None:
            raise ValueError("至少更正 observed_at 或 water_level 之一")
        return self


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    note: Optional[str] = Field(default=None, max_length=500)


class PublishConfirmRequest(BaseModel):
    """受控发布确认：以核对返回的凭证（token）完成确认，可选备注。"""

    model_config = ConfigDict(extra="ignore")
    token: str = Field(min_length=1, max_length=200, description="发布核对返回的确认凭证")
    note: Optional[str] = Field(default=None, max_length=500, description="发布备注（可选）")


class ThresholdVersionCreate(BaseModel):
    """防汛方案调整：登记某站的一版阈值（自生效时间起采用）。"""

    effective_at: datetime = Field(description="带时区的生效时间，如 2026-10-02T08:00:00+08:00")
    rise_threshold: FiniteFloat = Field(description="上升阈值（有限数值）")
    clear_threshold: FiniteFloat = Field(description="解除阈值（有限数值，必须严格小于上升阈值）")
    reason: str = Field(min_length=1, max_length=500, description="调整原因（必填）")

    _tz = field_validator("effective_at")(_require_tz_aware)

    @model_validator(mode="after")
    def _check_thresholds(self) -> "ThresholdVersionCreate":
        if not self.rise_threshold > self.clear_threshold:
            raise ValueError("上升阈值必须严格大于解除阈值")
        return self


class ZoneCreate(BaseModel):
    """登记防汛责任区：非空名称、带时区生效时间、互不重复的成员站 ID、触发站数 K。"""

    name: str = Field(min_length=1, max_length=200, description="责任区名称（非空）")
    effective_at: datetime = Field(description="带时区的生效时间，如 2026-10-02T08:00:00+08:00")
    station_ids: list[int] = Field(min_length=1, description="成员监测站 ID 列表（互不重复，至少 1 个）")
    trigger_count: int = Field(ge=1, description="触发站数 K（1 ≤ K ≤ 成员数）")

    _tz = field_validator("effective_at")(_require_tz_aware)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("名称不得为空白")
        return v

    @model_validator(mode="after")
    def _check_k_and_unique_members(self) -> "ZoneCreate":
        if len(self.station_ids) != len(set(self.station_ids)):
            raise ValueError("成员监测站 ID 不得重复")
        if self.trigger_count > len(self.station_ids):
            raise ValueError("触发站数 K 不得大于成员站数量")
        return self


class FeedConsumerCreate(BaseModel):
    """登记下游变更消费方：仅需一个非空名称，成功后服务端发放只展示一次的密钥。"""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=200, description="消费方名称（非空）")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("名称不得为空白")
        return v


class FeedAckRequest(BaseModel):
    """确认一批公开变更：仅接受该批末尾的发布编号（acknowledge_publication_id）。"""

    model_config = ConfigDict(extra="ignore")
    acknowledge_publication_id: int = Field(ge=1, description="本批拉取结果的末尾发布编号")
