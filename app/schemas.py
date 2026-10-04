"""请求 / 响应数据模型与输入校验。

非法数据统一由 Pydantic 抛出 ``RequestValidationError``，FastAPI 自动返回
HTTP 422（见 :mod:`app.main` 中对错误响应的统一格式处理）。
"""

from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentIn(BaseModel):
    """单个层段：一种材料的连续打印区间。"""

    model_config = ConfigDict(extra="forbid")

    material_id: str = Field(..., min_length=1, description="层段所用材料 ID")
    print_seconds: int = Field(..., ge=0, description="该层段打印耗时（秒）")
    volume_mm3: int = Field(..., ge=0, description="该层段耗材体积（mm^3，仅记录用）")


class JobIn(BaseModel):
    """一个打印作业，按顺序给出层段与交付截止时间。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, description="作业 ID")
    deadline_seconds: int = Field(..., ge=0, description="截止秒数（相对开工时刻 0）")
    segments: list[SegmentIn] = Field(
        ...,
        min_length=1,
        description="按打印顺序排列的层段（至少 1 段，无固定上限；长作业直接进入 CP-SAT 规划）",
    )


class PlanRequest(BaseModel):
    """POST /plans 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    jobs: list[JobIn] = Field(
        ...,
        min_length=2,
        max_length=8,
        description="2~8 个作业",
    )
    initial_material: str = Field(..., min_length=1, description="开机时喷头内的初始材料")
    # purge_matrix[from][to] = 从材料 from 切换到材料 to 需要排出的 purge 体积
    purge_matrix: Dict[str, Dict[str, int]] = Field(
        ...,
        description="材料切换 purge 体积矩阵（mm^3），键为材料 ID",
    )
    switch_seconds: int = Field(..., ge=0, description="每次材料切换的固定耗时（秒）")
    total_purge_capacity_mm3: int = Field(..., ge=0, description="全流程 purge 总容量（mm^3）")
    time_limit_seconds: Optional[float] = Field(
        default=None,
        ge=0.1,
        le=300.0,
        description="求解器时间上限（秒，可选，默认 15）",
    )

    @model_validator(mode="after")
    def _validate(self) -> "PlanRequest":
        # 作业 ID 唯一
        job_ids = [j.job_id for j in self.jobs]
        if len(set(job_ids)) != len(job_ids):
            raise ValueError("jobs 中 job_id 必须唯一")

        used_materials = {self.initial_material}
        for job in self.jobs:
            for seg in job.segments:
                used_materials.add(seg.material_id)

        # purge 矩阵条目必须为非负整数；同材料切换 purge 必须为 0
        for from_m, row in self.purge_matrix.items():
            if not isinstance(from_m, str) or not from_m:
                raise ValueError("purge_matrix 的键必须为非空字符串")
            for to_m, val in row.items():
                if not isinstance(to_m, str) or not to_m:
                    raise ValueError("purge_matrix 的键必须为非空字符串")
                if val < 0:
                    raise ValueError(
                        f"purge_matrix[{from_m}][{to_m}] 不能为负"
                    )
                if from_m == to_m and val != 0:
                    raise ValueError(
                        f"purge_matrix[{from_m}][{to_m}] 同材料切换必须为 0"
                    )

        # 被引用材料构成的子矩阵必须完整：每个来源材料行都必须存在，
        # 且包含每个目标材料列（允许出现未被引用的额外材料）。
        used = sorted(used_materials)
        for from_m in used:
            row = self.purge_matrix.get(from_m)
            if row is None:
                raise ValueError(f"purge_matrix 缺少来源材料行: {from_m}")
            missing = [to_m for to_m in used if to_m not in row]
            if missing:
                raise ValueError(
                    f"purge_matrix[{from_m}] 缺少目标材料列: " + ", ".join(missing)
                )
        return self


class ExecutedSegmentIn(BaseModel):
    """一条已完成层段的实测记录（按实际执行顺序提交）。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, description="层段所属作业 ID")
    segment_index: int = Field(..., ge=1, description="作业内层段序号，从 1 开始")
    start_seconds: int = Field(..., ge=0, description="实测开始时刻（相对开工时刻 0，秒）")
    end_seconds: int = Field(..., ge=0, description="实测结束时刻（相对开工时刻 0，秒）")
    purge_before_mm3: int = Field(
        ..., ge=0, description="该层段开始前实测排出的 purge 体积（mm^3）"
    )


class ExecutionAuditRequest(PlanRequest):
    """POST /executions/audit 的请求体。

    在 /plans 全部规划参数之外追加按实际执行顺序排列的已完成层段记录；
    /plans 的校验规则原样继承。跨记录的引用 / 重复 / 时序校验见
    ``_validate_executions``，非法记录统一返回 422。
    """

    executions: list[ExecutedSegmentIn] = Field(
        ...,
        description=(
            "按实际执行顺序排列的已完成层段（字段必填；允许传空数组，"
            "表示尚未开工，返回 IN_PROGRESS）"
        ),
    )

    @model_validator(mode="after")
    def _validate_executions(self) -> "ExecutionAuditRequest":
        known = {
            job.job_id: {k: seg for k, seg in enumerate(job.segments, start=1)}
            for job in self.jobs
        }
        seen: set[tuple[str, int]] = set()
        for pos, rec in enumerate(self.executions, start=1):
            segs = known.get(rec.job_id)
            if segs is None:
                raise ValueError(f"executions 第 {pos} 条引用了未知作业: {rec.job_id}")
            if rec.segment_index not in segs:
                raise ValueError(
                    f"executions 第 {pos} 条引用了未知层段: "
                    f"{rec.job_id}.{rec.segment_index}"
                )
            key = (rec.job_id, rec.segment_index)
            if key in seen:
                raise ValueError(
                    f"executions 第 {pos} 条为重复层段: "
                    f"{rec.job_id}.{rec.segment_index}"
                )
            seen.add(key)
            if rec.end_seconds < rec.start_seconds:
                raise ValueError(
                    f"executions 第 {pos} 条（{rec.job_id}.{rec.segment_index}）"
                    "实测起止倒置：end_seconds 不能小于 start_seconds"
                )
        return self


class ReplayedSegment(BaseModel):
    """执行回放中的单个已完成层段（保留实测耗时与空闲，不做紧密重排）。"""

    order: int = Field(..., description="在提交记录中的执行序号，从 1 开始")
    job_id: str
    segment_index: int
    material_id: str
    start_seconds: int = Field(..., description="实测开始时刻（秒）")
    end_seconds: int = Field(..., description="实测结束时刻（秒）")
    actual_print_seconds: int = Field(
        ..., description="实测打印耗时 = end_seconds - start_seconds"
    )
    planned_print_seconds: int = Field(..., description="请求参数中声明的打印耗时（秒）")
    purge_before_mm3: int = Field(..., description="段前实测 purge（mm^3）")
    required_purge_mm3: int = Field(
        ...,
        description="按上一段实际材料与本段材料查矩阵得到的需求 purge；同材料为 0",
    )
    switched: bool = Field(..., description="本段材料是否与上一段实际材料不同")
    switched_from_material: Optional[str] = Field(
        None,
        description="换料前喷头内材料；首段取自 initial_material；同材料时为 null",
    )
    gap_seconds: int = Field(
        ...,
        description="距上一已完成层段结束的实测间隔（首段为 start_seconds - 0）"
    )
    required_switch_seconds: int = Field(
        ..., description="换料时必须预留的 switch_seconds；不换料为 0"
    )
    idle_seconds: int = Field(
        ..., description="间隔中超出必需换料耗时的空闲（gap - required_switch_seconds）"
    )


class ActualSwitchRecord(BaseModel):
    """一次实测发生的材料切换（仅在材料实际变化时产生）。"""

    order: int = Field(..., description="实测切换序号，从 1 开始")
    from_material: str
    to_material: str
    after_execution_order: int = Field(
        ..., description="切换发生在第几条已完成层段之后；0 表示首段之前"
    )
    at_seconds: int = Field(..., description="上一已完成层段的实测结束时刻（首段前为 0）")
    next_start_seconds: int = Field(..., description="换料后首个层段的实测开始时刻")
    gap_seconds: int = Field(..., description="实测预留的换料+空闲间隔（秒）")
    required_switch_seconds: int = Field(..., description="必须预留的换料耗时（秒）")
    purge_before_mm3: int = Field(..., description="实测排出的 purge（mm^3）")
    required_purge_mm3: int = Field(..., description="矩阵要求的 purge（mm^3）")


class AuditViolation(BaseModel):
    """回放中发现的一处违规（按发生位置逐条给出）。"""

    code: str = Field(
        ...,
        description=(
            "OVERLAP（时间重叠）/ LAYER_ORDER（作业内层序非法）/ "
            "SWITCH_TIME（换料间隔不足）/ SAME_MATERIAL_PURGE（同材料段前 purge 非零）/ "
            "PURGE_INSUFFICIENT（段前 purge 低于矩阵需求）/ "
            "PURGE_OVER_CAPACITY（实测累计 purge 超容）"
        ),
    )
    execution_order: int = Field(..., description="违规层段在提交记录中的序号，从 1 开始")
    job_id: str
    segment_index: int
    message: str
    actual: Optional[int] = Field(None, description="实测值（适用时给出）")
    required: Optional[int] = Field(None, description="要求值（适用时给出）")


class CompletedJobTardiness(BaseModel):
    """已完工（全部层段均已完成）作业的实测逾期。"""

    job_id: str
    completion_seconds: int = Field(..., description="该作业最后层段的实测结束时刻")
    deadline_seconds: int
    tardy_seconds: int


class ExecutionAuditResponse(BaseModel):
    status: str = Field(
        ..., description="IN_PROGRESS（进行中）/ COMPLETED（完成）/ VIOLATED（违规）"
    )
    executed_segment_count: int = Field(..., description="已提交层段条数")
    total_segment_count: int = Field(..., description="请求参数中的层段总数")
    replay: list[ReplayedSegment] = Field(default_factory=list)
    switches: list[ActualSwitchRecord] = Field(default_factory=list)
    violations: list[AuditViolation] = Field(default_factory=list)
    violation_count: int = 0
    purge_used_mm3: int = Field(..., description="段前实测 purge 的累计扣容量值")
    purge_remaining_mm3: int = Field(
        ..., description="剩余 purge 容量（容量 - 实测已用；超容时为负）"
    )
    completed_job_tardiness: list[CompletedJobTardiness] = Field(default_factory=list)
    total_tardiness_seconds: int = Field(
        ..., description="已完工作业逾期之和；未完工前缀不计逾期"
    )
    makespan_seconds: int = Field(..., description="最后一个已完成层段的实测结束时刻")


class PlannedSegment(BaseModel):
    """输出计划中的单个层段（按实际执行顺序排列）。"""

    order: int = Field(..., description="全局执行序号，从 1 开始")
    job_id: str
    segment_index: int = Field(..., description="作业内层段序号，从 1 开始")
    material_id: str
    print_seconds: int
    volume_mm3: int
    start_seconds: int
    end_seconds: int


class SwitchRecord(BaseModel):
    """一次材料切换（含从初始材料开始的首次切换）。"""

    order: int = Field(..., description="切换序号，从 1 开始")
    from_material: Optional[str] = Field(
        ..., description="切换前材料；首次切换来自初始材料，仍显式给出"
    )
    to_material: str
    purge_mm3: int
    switch_seconds: int
    after_segment_order: int = Field(
        ...,
        description="该切换发生在第几号层段之后；0 表示在首个层段之前",
    )
    at_seconds: int = Field(..., description="切换开始时刻（秒）")


class JobTardiness(BaseModel):
    job_id: str
    completion_seconds: int
    deadline_seconds: int
    tardy_seconds: int


class PlanResponse(BaseModel):
    status: str = Field(
        ...,
        description="OPTIMAL / FEASIBLE / INFEASIBLE / UNKNOWN",
    )
    optimal: bool = Field(..., description="是否已证明最优（含字典序决胜已证最优）")
    objective: Optional[dict] = Field(
        None,
        description="目标值：总 purge、总逾期，以及决胜阶段是否已证字典序最小"
    )
    plan: list[PlannedSegment] = Field(default_factory=list)
    switches: list[SwitchRecord] = Field(default_factory=list)
    job_tardiness: list[JobTardiness] = Field(default_factory=list)
    purge_used_mm3: int = 0
    purge_remaining_mm3: Optional[int] = None
    total_tardiness_seconds: int = 0
    makespan_seconds: int = 0
    infeasible_reason: Optional[dict] = Field(
        None,
        description="无解原因：CAPACITY（容量）或 PRECEDENCE（前置冲突）"
    )
    solver_message: str = ""
