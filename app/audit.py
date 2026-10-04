"""POST /executions/audit 的执行回放核验。

与 :mod:`app.solver` 的紧密排产不同，核验**不重新排产**：层段按提交的
实际执行顺序逐段回放，实测起止时刻、空闲间隔与实测打印耗时原样保留，
仅检查执行过程是否违反物理 / 容量规则：

* **层序**（``LAYER_ORDER``）：同一作业已出现层序 1…k 后，下一条该作业的
  记录只能是 k+1——跨作业可任意交错，但各自层序必须严格递增连续；
* **重叠**（``OVERLAP``）：后一条层段的实测开始不得早于前一条已完成层段
  的实测结束（按实际执行顺序逐对检查即可覆盖全部时间重叠）；
* **换料耗时**（``SWITCH_TIME``）：材料相对上一已完成层段发生变化时，
  两者间隔必须至少容纳 ``switch_seconds``（首段相对时刻 0、材料相对
  初始材料检查）；
* **段前 purge**：换料时段前实测 purge 不得低于矩阵需求
  （``PURGE_INSUFFICIENT``）；同材料段前 purge 必须为 0
  （``SAME_MATERIAL_PURGE``）；容量始终按**实测值**扣减，累计超容记
  ``PURGE_OVER_CAPACITY``。

任一违规都会使整体状态为 ``VIOLATED`` 且不能标记完成；层段全部完成且
无违规才是 ``COMPLETED``，否则为 ``IN_PROGRESS``。未完工前缀不计逾期：
只有某作业全部层段都出现在记录中时，才按其最后层段实测结束时刻核算
逾期。

未知 / 重复层段、起止倒置属于请求本身非法，由 schema 校验拦截为 422，
不进入本模块。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .schemas import ExecutionAuditRequest
from .solver import Instance, build_instance


def audit_execution(req: ExecutionAuditRequest) -> dict:
    """逐段回放已完成层段，返回可直接序列化为 ExecutionAuditResponse 的字典。"""
    inst = build_instance(req)  # type: ignore[arg-type]

    # 按 (作业 ID, 层序) 取全局层段属性
    by_rank: Dict[Tuple[str, int], int] = {}
    for gi in range(1, inst.n + 1):
        by_rank[(inst.job_of[gi], inst.seg_index_of[gi])] = gi

    violations: List[dict] = []

    def add_violation(
        code: str,
        order: int,
        gi: int,
        message: str,
        *,
        actual: Optional[int] = None,
        required: Optional[int] = None,
    ) -> None:
        violations.append(
            {
                "code": code,
                "execution_order": order,
                "job_id": inst.job_of[gi],
                "segment_index": inst.seg_index_of[gi],
                "message": message,
                "actual": actual,
                "required": required,
            }
        )

    replay: List[dict] = []
    switches: List[dict] = []
    purge_used = 0

    # 每个作业已出现的最大连续层序（None 表示尚未出现任何层段）
    next_index: Dict[str, Optional[int]] = {jd.job_id: 1 for jd in inst.jobs}
    seen_per_job: Dict[str, set] = {jd.job_id: set() for jd in inst.jobs}

    prev_end = 0
    prev_material: Optional[str] = None  # None 表示尚无已完成层段
    prev_order = 0

    for order, rec in enumerate(req.executions, start=1):
        gi = by_rank[(rec.job_id, rec.segment_index)]
        material = inst.material_of[gi]
        base_material = inst.initial_material if prev_material is None else prev_material
        switched = material != base_material
        required_purge = (
            int(inst.purge_matrix[base_material][material]) if switched else 0
        )
        required_switch = inst.switch_seconds if switched else 0
        gap = rec.start_seconds - prev_end
        idle = gap - required_switch

        # 1) 作业内层序：必须恰好是该作业下一个未完成层序
        expected = next_index[rec.job_id]
        if expected is None or rec.segment_index != expected:
            if expected is not None and expected > 1:
                prev_desc = str(expected - 1)
                next_desc = str(expected)
            else:
                prev_desc = "无（该作业尚未开始）"
                next_desc = "1"
            add_violation(
                "LAYER_ORDER",
                order,
                gi,
                (
                    f"作业 {rec.job_id} 层序非法：上一已完成层段为 {prev_desc}，"
                    f"下一段必须为 {next_desc}，实际提交 {rec.segment_index}"
                    "（跨作业可交错，但各作业层序须严格连续递增）"
                ),
                actual=rec.segment_index,
                required=expected,
            )

        # 2) 时间重叠：相对上一已完成层段（按实际执行顺序）
        if gap < 0:
            add_violation(
                "OVERLAP",
                order,
                gi,
                (
                    f"层段实测开始 {rec.start_seconds}s 早于上一已完成层段结束 "
                    f"{prev_end}s，时间重叠 {abs(gap)}s"
                ),
                actual=rec.start_seconds,
                required=prev_end,
            )

        # 3) 换料耗时：换料前须留足 switch_seconds
        if switched and gap < required_switch:
            add_violation(
                "SWITCH_TIME",
                order,
                gi,
                (
                    f"材料由 {base_material} 换为 {material}，段前实测间隔仅 "
                    f"{gap}s，不足 switch_seconds={required_switch}s"
                ),
                actual=gap,
                required=required_switch,
            )

        # 4) 段前 purge：同材料必须为 0；换料不得低于矩阵需求
        if not switched and rec.purge_before_mm3 != 0:
            add_violation(
                "SAME_MATERIAL_PURGE",
                order,
                gi,
                (
                    f"本段材料 {material} 与上一段相同，同材料段前 purge 必须为 0，"
                    f"实测 {rec.purge_before_mm3} mm^3"
                ),
                actual=rec.purge_before_mm3,
                required=0,
            )
        if switched and rec.purge_before_mm3 < required_purge:
            add_violation(
                "PURGE_INSUFFICIENT",
                order,
                gi,
                (
                    f"材料由 {base_material} 换为 {material}，段前实测 purge "
                    f"{rec.purge_before_mm3} mm^3 低于矩阵需求 "
                    f"{required_purge} mm^3"
                ),
                actual=rec.purge_before_mm3,
                required=required_purge,
            )

        # 容量按实测值扣减（同材料的非零 purge 同样占用容量）
        purge_used += rec.purge_before_mm3
        if purge_used > inst.purge_capacity:
            add_violation(
                "PURGE_OVER_CAPACITY",
                order,
                gi,
                (
                    f"段前 purge 后累计实测消耗 {purge_used} mm^3，"
                    f"超过总容量 {inst.purge_capacity} mm^3"
                ),
                actual=purge_used,
                required=inst.purge_capacity,
            )

        if switched:
            switches.append(
                {
                    "order": len(switches) + 1,
                    "from_material": base_material,
                    "to_material": material,
                    "after_execution_order": prev_order,
                    "at_seconds": prev_end,
                    "next_start_seconds": rec.start_seconds,
                    "gap_seconds": gap,
                    "required_switch_seconds": required_switch,
                    "purge_before_mm3": rec.purge_before_mm3,
                    "required_purge_mm3": required_purge,
                }
            )

        replay.append(
            {
                "order": order,
                "job_id": rec.job_id,
                "segment_index": rec.segment_index,
                "material_id": material,
                "start_seconds": rec.start_seconds,
                "end_seconds": rec.end_seconds,
                "actual_print_seconds": rec.end_seconds - rec.start_seconds,
                "planned_print_seconds": inst.duration_of[gi],
                "purge_before_mm3": rec.purge_before_mm3,
                "required_purge_mm3": required_purge,
                "switched": switched,
                "switched_from_material": base_material if switched else None,
                "gap_seconds": gap,
                "required_switch_seconds": required_switch,
                "idle_seconds": idle,
            }
        )

        # 推进回放状态（即使存在违规，也按实测记录继续，以便完整报告后续问题）
        seen_per_job[rec.job_id].add(rec.segment_index)
        if rec.segment_index == next_index[rec.job_id]:
            job = next(jd for jd in inst.jobs if jd.job_id == rec.job_id)
            if rec.segment_index >= len(job.segments):
                next_index[rec.job_id] = None  # 该作业层段已全部出现
            else:
                next_index[rec.job_id] = rec.segment_index + 1
        prev_end = rec.end_seconds
        prev_material = material
        prev_order = order

    # ---- 完工作业逾期：仅统计全部层段均已出现的作业 ----
    completed_at: Dict[str, int] = {}
    for jd in inst.jobs:
        if len(seen_per_job[jd.job_id]) == len(jd.segments):
            # 全部层段出现时，最后层段（last_idx）的实测结束即完工时刻
            last_index = inst.seg_index_of[jd.last_idx]
            for rec in req.executions:
                if rec.job_id == jd.job_id and rec.segment_index == last_index:
                    completed_at[jd.job_id] = rec.end_seconds
                    break

    completed_job_tardiness = []
    total_tardiness = 0
    for jd in inst.jobs:
        if jd.job_id in completed_at:
            comp = completed_at[jd.job_id]
            tardy = max(0, comp - jd.deadline)
            total_tardiness += tardy
            completed_job_tardiness.append(
                {
                    "job_id": jd.job_id,
                    "completion_seconds": comp,
                    "deadline_seconds": jd.deadline,
                    "tardy_seconds": tardy,
                }
            )

    executed = len(req.executions)
    if violations:
        status = "VIOLATED"
    elif executed == inst.n:
        status = "COMPLETED"
    else:
        status = "IN_PROGRESS"

    return {
        "status": status,
        "executed_segment_count": executed,
        "total_segment_count": inst.n,
        "replay": replay,
        "switches": switches,
        "violations": violations,
        "violation_count": len(violations),
        "purge_used_mm3": purge_used,
        "purge_remaining_mm3": inst.purge_capacity - purge_used,
        "completed_job_tardiness": completed_job_tardiness,
        "total_tardiness_seconds": total_tardiness,
        "makespan_seconds": prev_end if replay else 0,
    }
