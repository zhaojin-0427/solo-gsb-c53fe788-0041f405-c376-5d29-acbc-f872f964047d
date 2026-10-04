"""端到端测试：POST /executions/audit 执行回放核验。

覆盖：完成 / 进行中 / 违规三态、空闲与实测耗时保留、换料耗时与段前
purge 规则、跨作业交错、层序 / 重叠违规、已完工逾期与前缀不计逾期、
422（未知 / 重复层段、起止倒置、继承的 /plans 校验）。
"""

import json
import os

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

HERE = os.path.dirname(__file__)


def load_example(name: str) -> dict:
    with open(os.path.join(HERE, "..", "examples", name)) as f:
        return json.load(f)


def audit(payload: dict):
    return client.post("/executions/audit", json=payload)


def rec(job_id, seg, start, end, purge):
    return {
        "job_id": job_id,
        "segment_index": seg,
        "start_seconds": start,
        "end_seconds": end,
        "purge_before_mm3": purge,
    }


def base_payload() -> dict:
    """examples/request.json：J1(PLA60,ABS30,dl120) + J2(ABS80,PLA20,dl220)，
    初始 PLA，PLA->ABS=120、ABS->PLA=80，switch=10，容量 400。"""
    return load_example("request.json")


# ------------------------------- 完成 / 进行中 -------------------------------

def test_completed_tight_replay():
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 60, 0),
        rec("J1", 2, 70, 100, 120),
        rec("J2", 1, 100, 180, 0),
        rec("J2", 2, 190, 210, 80),
    ]
    d = audit(payload).json()
    assert d["status"] == "COMPLETED"
    assert d["violations"] == []
    assert d["executed_segment_count"] == 4
    assert d["total_segment_count"] == 4
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == 200
    assert d["total_tardiness_seconds"] == 0
    assert d["makespan_seconds"] == 210
    assert [(s["from_material"], s["to_material"], s["required_purge_mm3"]) for s in d["switches"]] == [
        ("PLA", "ABS", 120),
        ("ABS", "PLA", 80),
    ]
    # 回放明细：换料标记与间隔
    rows = d["replay"]
    assert rows[0]["switched"] is False
    assert rows[0]["switched_from_material"] is None
    assert rows[1]["switched"] is True
    assert rows[1]["switched_from_material"] == "PLA"
    assert rows[1]["gap_seconds"] == 10
    assert rows[1]["required_switch_seconds"] == 10
    assert rows[1]["idle_seconds"] == 0
    # 完工作业逾期明细齐全且均为 0
    assert {j["job_id"] for j in d["completed_job_tardiness"]} == {"J1", "J2"}


def test_replay_preserves_idle_and_actual_duration():
    """空闲间隔与实测耗时原样保留，不按紧密排产重算。"""
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 62, 0),     # 实测 62s（计划 60）
        rec("J1", 2, 80, 115, 120),  # 间隔 18 = 换料 10 + 空闲 8；实测 35s（计划 30）
        rec("J2", 1, 130, 210, 0),   # 同材料间隔 15 全为空闲
        rec("J2", 2, 225, 250, 80),  # 间隔 15 = 换料 10 + 空闲 5
    ]
    d = audit(payload).json()
    assert d["status"] == "COMPLETED", d["violations"]
    rows = d["replay"]
    assert rows[0]["actual_print_seconds"] == 62
    assert rows[0]["planned_print_seconds"] == 60
    assert rows[1]["actual_print_seconds"] == 35
    assert rows[1]["gap_seconds"] == 18
    assert rows[1]["idle_seconds"] == 8
    assert rows[2]["gap_seconds"] == 15
    assert rows[2]["required_switch_seconds"] == 0
    assert rows[2]["idle_seconds"] == 15
    assert rows[3]["idle_seconds"] == 5
    # makespan 取实测值，绝不重算
    assert d["makespan_seconds"] == 250
    assert d["total_tardiness_seconds"] == 30  # J1 115<=120；J2 250-220
    j2 = next(j for j in d["completed_job_tardiness"] if j["job_id"] == "J2")
    assert j2["tardy_seconds"] == 30
    assert j2["completion_seconds"] == 250


def test_in_progress_prefix_not_counted_as_tardy():
    """未完工前缀：无违规即 IN_PROGRESS，未完工作业不计逾期。"""
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 60, 0),
        rec("J2", 1, 70, 300, 120),  # J2.1 远超两作业截止，但 J2 未完工
    ]
    d = audit(payload).json()
    assert d["status"] == "IN_PROGRESS"
    assert d["executed_segment_count"] == 2
    assert d["total_segment_count"] == 4
    assert d["completed_job_tardiness"] == []
    assert d["total_tardiness_seconds"] == 0
    assert d["purge_used_mm3"] == 120
    assert d["purge_remaining_mm3"] == 280
    assert d["makespan_seconds"] == 300


def test_in_progress_with_one_completed_tardy_job():
    """整体未完工，但已完工的作业逾期要计入。"""
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 90, 0),
        rec("J1", 2, 100, 130, 120),  # J1 全段完工，130 > deadline 120
    ]
    d = audit(payload).json()
    assert d["status"] == "IN_PROGRESS"
    assert [j["job_id"] for j in d["completed_job_tardiness"]] == ["J1"]
    assert d["total_tardiness_seconds"] == 10


def test_empty_executions_is_in_progress():
    payload = base_payload()
    payload["executions"] = []
    d = audit(payload).json()
    assert d["status"] == "IN_PROGRESS"
    assert d["replay"] == []
    assert d["switches"] == []
    assert d["purge_used_mm3"] == 0
    assert d["purge_remaining_mm3"] == 400
    assert d["makespan_seconds"] == 0


def test_cross_job_interleaving_legal():
    """跨作业交错合法，只要各作业层序递增且换料规则满足。"""
    payload = base_payload()
    # J1.1(PLA) -> J2.1(ABS) -> J1.2(ABS) -> J2.2(PLA)
    payload["executions"] = [
        rec("J1", 1, 0, 60, 0),
        rec("J2", 1, 70, 150, 120),
        rec("J1", 2, 150, 180, 0),
        rec("J2", 2, 190, 210, 80),
    ]
    d = audit(payload).json()
    assert d["status"] == "COMPLETED", d["violations"]
    assert [(r["job_id"], r["segment_index"]) for r in d["replay"]] == [
        ("J1", 1), ("J2", 1), ("J1", 2), ("J2", 2)
    ]
    assert d["purge_used_mm3"] == 200


def test_initial_material_switch_checked_from_time_zero():
    """首段材料与初始材料不同：时刻 0 起须留足 switch_seconds 与矩阵 purge。"""
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100, "segments": [
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "B", "print_seconds": 10, "volume_mm3": 1}]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0, "B": 50}, "B": {"A": 1, "B": 0}},
        "switch_seconds": 3,
        "total_purge_capacity_mm3": 1000,
    }
    # 合法：3s 换料 + 50 purge
    ok = dict(payload, executions=[
        rec("J1", 1, 3, 13, 50),
        rec("J2", 1, 13, 23, 0),
    ])
    d = audit(ok).json()
    assert d["status"] == "COMPLETED", d["violations"]
    assert d["replay"][0]["gap_seconds"] == 3
    assert d["switches"][0]["after_execution_order"] == 0

    # 违规：仅留 2s
    bad = dict(payload, executions=[rec("J1", 1, 2, 12, 50)])
    d = audit(bad).json()
    assert d["status"] == "VIOLATED"
    assert any(v["code"] == "SWITCH_TIME" and v["execution_order"] == 1 for v in d["violations"])

    # 违规：purge 不足 50
    bad2 = dict(payload, executions=[rec("J1", 1, 3, 13, 40)])
    d = audit(bad2).json()
    assert any(v["code"] == "PURGE_INSUFFICIENT" for v in d["violations"])


# --------------------------------- 违规 ---------------------------------

def test_overlap_violation():
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 60, 0),
        rec("J2", 1, 55, 140, 120),  # 早于上一段结束 60
    ]
    d = audit(payload).json()
    assert d["status"] == "VIOLATED"
    codes = {v["code"] for v in d["violations"]}
    assert "OVERLAP" in codes
    v = next(v for v in d["violations"] if v["code"] == "OVERLAP")
    assert v["execution_order"] == 2
    assert v["job_id"] == "J2" and v["segment_index"] == 1
    assert v["actual"] == 55 and v["required"] == 60


def test_layer_order_violation_cannot_complete():
    """层序错误（跳段 / 乱序）记 LAYER_ORDER，即使层段齐了也不能标完成。"""
    payload = base_payload()
    # 直接提交 J1.2（缺 J1.1）
    payload["executions"] = [rec("J1", 2, 10, 40, 120)]
    d = audit(payload).json()
    assert d["status"] == "VIOLATED"
    v = next(v for v in d["violations"] if v["code"] == "LAYER_ORDER")
    assert v["execution_order"] == 1
    assert v["actual"] == 2 and v["required"] == 1
    # J1 只有 1 条记录，不算完工
    assert d["completed_job_tardiness"] == []


def test_layer_order_skip_midway_cannot_complete():
    """四条层段都提交但某作业跳过层序：违规且不标 COMPLETED。"""
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 60, 0),
        rec("J1", 2, 70, 100, 120),
        rec("J2", 1, 100, 180, 0),
        # J2.1 之后直接再次提交 J2.1 会 422；这里构造 J2 只有两段，
        # 改为 J1 层序错误：J1.1 之后先给 J1.1 不可能重复，故用三作业场景
    ]
    # 改用更直接的跳段：作业内 1 之后给 3
    three = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 1000, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1},
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1},
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1}]},
            {"job_id": "J2", "deadline_seconds": 1000, "segments": [
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1}]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0}},
        "switch_seconds": 0,
        "total_purge_capacity_mm3": 0,
        "executions": [
            rec("J1", 1, 0, 10, 0),
            rec("J1", 3, 10, 20, 0),  # 跳过 J1.2
            rec("J2", 1, 20, 30, 0),
        ],
    }
    d = audit(three).json()
    assert d["status"] == "VIOLATED"
    assert any(v["code"] == "LAYER_ORDER" and v["execution_order"] == 2 for v in d["violations"])
    assert d["executed_segment_count"] == 3
    assert d["total_segment_count"] == 4


def test_switch_time_violation():
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 60, 0),
        rec("J1", 2, 65, 95, 120),  # 间隔 5 < switch_seconds 10
    ]
    d = audit(payload).json()
    assert d["status"] == "VIOLATED"
    v = next(v for v in d["violations"] if v["code"] == "SWITCH_TIME")
    assert v["actual"] == 5 and v["required"] == 10


def test_same_material_purge_must_be_zero():
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 60, 7),       # 首段 PLA==初始材料，purge 必须为 0
        rec("J1", 2, 70, 100, 120),
        rec("J2", 1, 100, 180, 3),    # ABS 同材料段前 purge 必须为 0
        rec("J2", 2, 190, 210, 80),
    ]
    d = audit(payload).json()
    assert d["status"] == "VIOLATED"
    same = [v for v in d["violations"] if v["code"] == "SAME_MATERIAL_PURGE"]
    assert {(v["execution_order"], v["actual"]) for v in same} == {(1, 7), (3, 3)}
    # 实测值照样扣容量
    assert d["purge_used_mm3"] == 7 + 120 + 3 + 80
    assert d["purge_remaining_mm3"] == 400 - 210


def test_purge_insufficient_violation():
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 60, 0),
        rec("J1", 2, 70, 100, 100),  # 需求 120，实测 100
    ]
    d = audit(payload).json()
    assert d["status"] == "VIOLATED"
    v = next(v for v in d["violations"] if v["code"] == "PURGE_INSUFFICIENT")
    assert v["actual"] == 100 and v["required"] == 120
    # 容量按实测扣减（而非按需求）
    assert d["purge_used_mm3"] == 100


def test_purge_over_capacity_violation():
    payload = base_payload()
    payload["total_purge_capacity_mm3"] = 190
    payload["executions"] = [
        rec("J1", 1, 0, 60, 0),
        rec("J1", 2, 70, 100, 120),
        rec("J2", 1, 100, 180, 0),
        rec("J2", 2, 190, 210, 80),  # 累计 200 > 190
    ]
    d = audit(payload).json()
    assert d["status"] == "VIOLATED"
    v = next(v for v in d["violations"] if v["code"] == "PURGE_OVER_CAPACITY")
    assert v["execution_order"] == 4
    assert v["actual"] == 200 and v["required"] == 190
    assert d["purge_used_mm3"] == 200
    assert d["purge_remaining_mm3"] == -10


def test_violated_reports_each_position_and_keeps_tardiness():
    """多个违规逐段累积报告；已完工作业逾期仍如实给出。"""
    payload = base_payload()
    payload["executions"] = [
        rec("J1", 1, 0, 60, 0),
        rec("J1", 2, 65, 95, 90),    # SWITCH_TIME(5<10) + PURGE_INSUFFICIENT(90<120)
        rec("J2", 1, 90, 170, 0),    # OVERLAP（90 < 95）
        rec("J2", 2, 175, 230, 80),  # SWITCH_TIME（5<10）；J2 完工 230 逾期 10
    ]
    d = audit(payload).json()
    assert d["status"] == "VIOLATED"
    by_order = {}
    for v in d["violations"]:
        by_order.setdefault(v["execution_order"], []).append(v["code"])
    assert set(by_order[2]) == {"SWITCH_TIME", "PURGE_INSUFFICIENT"}
    assert by_order[3] == ["OVERLAP"]
    assert by_order[4] == ["SWITCH_TIME"]
    assert d["violation_count"] == 4
    # 全部层段出现：两作业完工，逾期照算，但整体仍 VIOLATED 而非 COMPLETED
    assert d["total_tardiness_seconds"] == 10
    assert {j["job_id"] for j in d["completed_job_tardiness"]} == {"J1", "J2"}


def test_zero_duration_segments_allowed():
    """零耗时层段合法：end == start 可与前段结束齐平（gap=0）。"""
    payload = {
        "jobs": [
            {"job_id": "J1", "deadline_seconds": 100, "segments": [
                {"material_id": "A", "print_seconds": 0, "volume_mm3": 0},
                {"material_id": "A", "print_seconds": 10, "volume_mm3": 1}]},
            {"job_id": "J2", "deadline_seconds": 100, "segments": [
                {"material_id": "A", "print_seconds": 5, "volume_mm3": 1}]},
        ],
        "initial_material": "A",
        "purge_matrix": {"A": {"A": 0}},
        "switch_seconds": 0,
        "total_purge_capacity_mm3": 0,
        "executions": [
            rec("J1", 1, 0, 0, 0),
            rec("J1", 2, 0, 10, 0),
            rec("J2", 1, 10, 15, 0),
        ],
    }
    d = audit(payload).json()
    assert d["status"] == "COMPLETED", d["violations"]
    assert d["replay"][0]["actual_print_seconds"] == 0


# --------------------------------- 422 ---------------------------------

def test_422_cases():
    base = base_payload()
    cases = {
        "unknown job": dict(base, executions=[rec("ZZ", 1, 0, 1, 0)]),
        "unknown segment": dict(base, executions=[rec("J1", 9, 0, 1, 0)]),
        "duplicate segment": dict(base, executions=[
            rec("J1", 1, 0, 10, 0), rec("J1", 1, 10, 20, 0)]),
        "inverted times": dict(base, executions=[rec("J1", 1, 50, 10, 0)]),
        "negative purge": dict(base, executions=[
            {"job_id": "J1", "segment_index": 1, "start_seconds": 0,
             "end_seconds": 10, "purge_before_mm3": -1}]),
        "segment index zero": dict(base, executions=[
            {"job_id": "J1", "segment_index": 0, "start_seconds": 0,
             "end_seconds": 10, "purge_before_mm3": 0}]),
        "missing executions field": {k: v for k, v in base.items()},
        "extra field": dict(base, executions=[
            {"job_id": "J1", "segment_index": 1, "start_seconds": 0,
             "end_seconds": 10, "purge_before_mm3": 0, "note": "x"}]),
        # 继承自 /plans 的校验
        "one job": dict(base, jobs=base["jobs"][:1], executions=[]),
        "dup job id": dict(base, jobs=[
            dict(base["jobs"][0], job_id="X"),
            dict(base["jobs"][1], job_id="X")], executions=[]),
        "missing matrix row": dict(base, initial_material="PETG", executions=[]),
    }
    for name, payload in cases.items():
        r = audit(payload)
        assert r.status_code == 422, (name, r.status_code, r.text)


def test_missing_body_422():
    r = client.post("/executions/audit")
    assert r.status_code == 422


def test_plans_endpoint_unchanged():
    """原有 /plans 行为不受影响。"""
    d = client.post("/plans", json=base_payload()).json()
    assert d["status"] == "OPTIMAL"
    assert d["purge_used_mm3"] == 200
