# 多材料 3D 打印换料规划与执行核验 API

基于 **Python + FastAPI + Google OR-Tools（CP-SAT）** 的排产服务，提供两个接口：

- **`POST /plans`**：给定 2～8 个打印作业（每个作业由若干**按顺序执行的层段**组成）、
  初始材料、材料切换 purge 体积矩阵、单次切换耗时与总 purge 容量，计算层段交错执行计划；
- **`POST /executions/audit`**：提交同一份规划参数和按**实际执行顺序**记录的已完成层段
  （作业 ID、层序、实测起止秒、段前实测 purge），服务**保留空闲与实际耗时、不重新排产**，
  逐段回放并核验换料间隔 / 段前 purge / 容量 / 层序 / 时间重叠，返回进行中、完成或违规状态。

规划接口在 purge 容量与作业截止时间约束下：

- 作业之间**只能在层段边界交错**，层段不可拆分，作业内层段严格按序；
- 每次材料切换按 `purge_matrix[from][to]` 消耗 purge 容量，并占用 `switch_seconds` 秒
  （首层段材料与初始材料不同时，也要先切换再打印）；同材料相邻无需切换；
- 作业逾期 = `max(0, 该作业最后层段完成时刻 - 截止秒数)`；
- purge 总消耗超过 `total_purge_capacity_mm3` 即**无解**。

优化目标按严格优先级依次为：

1. **总 purge 体积最小**；
2. 在 purge 最优的前提下，**总逾期秒数最小**；
3. 前两者相同时，按执行序列的 `(作业 ID, 层序)` 取**字典序最小**计划
   （作业 ID 字符串升序，再按作业内层段序号升序，逐位比较）。

返回切换记录、purge 余量、逐作业逾期明细与求解状态；未在限时内证明最优时
**不会**标记为最优。

---

## 一、Docker Compose 一键启动

前置要求：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build -d
```

服务监听在宿主机 `http://localhost:8000`，两个接口均可直接访问：

- 规划：`POST http://localhost:8000/plans`
- 执行核验：`POST http://localhost:8000/executions/audit`

查看日志 / 停止：

```bash
docker compose logs -f
docker compose down
```

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

交互式 API 文档（浏览器打开）：

- Swagger UI：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc

## 二、本地直接运行（可选）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install pytest
pytest -q
```

## 三、调用接口

- 规划：[`POST /plans`](#post-plans)
- 执行核验：[`POST /executions/audit`](#post-executionsaudit执行核验)

### `POST /plans`

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `jobs` | 数组（2～8 项） | 作业列表 |
| `jobs[].job_id` | 非空字符串 | 作业 ID，全局唯一 |
| `jobs[].deadline_seconds` | 非负整数 | 截止秒数（相对开工时刻 0） |
| `jobs[].segments` | 非空数组（至少 1 段，无固定上限） | 按打印顺序排列的层段；单作业 25 段及以上的长作业同样进入 CP-SAT 规划 |
| `segments[].material_id` | 非空字符串 | 层段材料 ID |
| `segments[].print_seconds` | 非负整数 | 层段打印秒数 |
| `segments[].volume_mm3` | 非负整数 | 层段耗材体积 mm³（记录用） |
| `initial_material` | 非空字符串 | 喷头初始材料 |
| `purge_matrix` | 对象 | `{from: {to: purge体积}}`，非负整数；对角必须为 0；所有被引用材料（含初始材料）构成的子矩阵行列必须完整 |
| `switch_seconds` | 非负整数 | 每次材料切换的固定耗时（秒） |
| `total_purge_capacity_mm3` | 非负整数 | 全流程 purge 总容量（mm³） |
| `time_limit_seconds` | 数值，0.1～300，可选 | 求解器时间上限，默认 15 秒 |

请求示例（文件见 `examples/request.json`）：

```json
{
  "jobs": [
    {
      "job_id": "J1",
      "deadline_seconds": 120,
      "segments": [
        {"material_id": "PLA", "print_seconds": 60, "volume_mm3": 100},
        {"material_id": "ABS", "print_seconds": 30, "volume_mm3": 40}
      ]
    },
    {
      "job_id": "J2",
      "deadline_seconds": 220,
      "segments": [
        {"material_id": "ABS", "print_seconds": 80, "volume_mm3": 120},
        {"material_id": "PLA", "print_seconds": 20, "volume_mm3": 30}
      ]
    }
  ],
  "initial_material": "PLA",
  "purge_matrix": {
    "PLA": {"PLA": 0, "ABS": 120},
    "ABS": {"PLA": 80, "ABS": 0}
  },
  "switch_seconds": 10,
  "total_purge_capacity_mm3": 400,
  "time_limit_seconds": 15
}
```

调用：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

**响应（与服务实际输出一致）**：

```json
{
  "status": "OPTIMAL",
  "optimal": true,
  "objective": {
    "total_purge_mm3": 200,
    "total_tardiness_seconds": 0,
    "purge_optimal": true,
    "tardiness_optimal": true,
    "lexicographic_minimal": true
  },
  "plan": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "PLA",
     "print_seconds": 60, "volume_mm3": 100, "start_seconds": 0, "end_seconds": 60},
    {"order": 2, "job_id": "J1", "segment_index": 2, "material_id": "ABS",
     "print_seconds": 30, "volume_mm3": 40, "start_seconds": 70, "end_seconds": 100},
    {"order": 3, "job_id": "J2", "segment_index": 1, "material_id": "ABS",
     "print_seconds": 80, "volume_mm3": 120, "start_seconds": 100, "end_seconds": 180},
    {"order": 4, "job_id": "J2", "segment_index": 2, "material_id": "PLA",
     "print_seconds": 20, "volume_mm3": 30, "start_seconds": 190, "end_seconds": 210}
  ],
  "switches": [
    {"order": 1, "from_material": "PLA", "to_material": "ABS",
     "purge_mm3": 120, "switch_seconds": 10,
     "after_segment_order": 1, "at_seconds": 60},
    {"order": 2, "from_material": "ABS", "to_material": "PLA",
     "purge_mm3": 80, "switch_seconds": 10,
     "after_segment_order": 3, "at_seconds": 180}
  ],
  "job_tardiness": [
    {"job_id": "J1", "completion_seconds": 100, "deadline_seconds": 120, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 210, "deadline_seconds": 220, "tardy_seconds": 0}
  ],
  "purge_used_mm3": 200,
  "purge_remaining_mm3": 200,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 210,
  "infeasible_reason": null,
  "solver_message": "已证明三级目标（purge、逾期、字典序）均最优"
}
```

结果解读：最优排产把两个 ABS 层段相邻（J1.2 后紧跟 J2.1，免一次切换），
全流程仅 PLA→ABS（120 mm³）与 ABS→PLA（80 mm³）两次切换，共 200 mm³，
两个作业均按期完成，purge 余量 200 mm³。

### 无解响应（容量超限）

将 `examples/request_infeasible.json` 中容量改为 150 mm³：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_infeasible.json
```

```json
{
  "status": "INFEASIBLE",
  "optimal": false,
  "objective": null,
  "plan": [],
  "switches": [],
  "job_tardiness": [],
  "purge_used_mm3": 0,
  "purge_remaining_mm3": null,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 0,
  "infeasible_reason": {
    "code": "CAPACITY",
    "message": "任何可行排产至少需要 purge 200 mm^3，超过总容量 150 mm^3",
    "minimum_required_purge_mm3": 200,
    "capacity_mm3": 150
  },
  "solver_message": "模型无解，归因: CAPACITY"
}
```

无解归因 `infeasible_reason.code`：

- `CAPACITY`：层段先后本身可排，但最小 purge 需求仍超过容量（响应给出
  `minimum_required_purge_mm3`）；
- `PRECEDENCE`：层段前置约束冲突（本服务层序由输入保证，正常数据下不会出现，
  仅作为防御性归因）；
- `UNKNOWN`：限时内无法完成归因诊断。

### 长作业（单作业 25 段及以上）

层段数量没有固定上限：单个作业包含 25 个或更多按序层段时是**合法请求**，
不会被拒绝（HTTP 422 仅针对真正非法的输入），而是直接进入 OR-Tools
CP-SAT 规划。示例 `examples/request_long_job.json` 中 `LONG` 作业含 25 个
按序层段（PLA/ABS/TPU 循环）：

```bash
docker compose up --build -d

curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_long_job.json
```

响应与普通请求结构完全一致：`plan` 覆盖全部层段且每个作业内的
`segment_index` 严格按 1…N 排列，`switches` / `job_tardiness` /
`purge_used_mm3` / `purge_remaining_mm3` 等明细同样完整返回；三级目标
全部由 CP-SAT 证明时才标记 `"status": "OPTIMAL"`。

当模型较大或 `time_limit_seconds` 到期时：

- CP-SAT 已给出可行解但未完成最优性证明 → `FEASIBLE`，`objective` 中
  `purge_optimal` / `tardiness_optimal` / `lexicographic_minimal` 分别
  如实反映各级目标**是否确已证明**（可能 purge 已证最优而字典序未证明）；
- CP-SAT 连可行解都未找到 → 服务用免求解的**构造性排列**兜底：除整作业
  串行外，还把每个作业切成同材料连续段（run）并**按材料交错**，让不同
  作业的同材料 run 相邻执行（两个均按 A→B→A 用料的作业，串行至少换料
  4 次，按材料交错只需 2 次）。候选严格遵守作业内层段顺序，purge 经
  精确核算，仅取不超容者并按（purge、逾期、字典序）择优，仍返回完整、
  按序的计划与全部明细，状态为 `FEASIBLE`，且三个最优性标记均为 `false`；
- 构造性候选也不满足 purge 容量（或确属容量无解）→ `UNKNOWN` /
  `INFEASIBLE`，此时 `plan` 为空，不会伪造计划。

可用极短时限观察该行为（大模型通常来不及证明最优）：

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_long_job.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["objective"])'
# OPTIMAL {'total_purge_mm3': 2320, ..., 'lexicographic_minimal': True}
```

### 按材料交错的长作业（A→B→A 用料）

`examples/request_interleave.json` 是需要跨作业交错才能满足容量的典型场景：
两个 25 段长作业均按 A→B→A 用料（9 段 A、8 段 B、8 段 A，允许同材料连续
多段），初始材料 A，跨材料 purge 均为 1 mm³，总容量仅 2 mm³：

- **整作业串行**（J1 打完再打 J2）至少换料 4 次（每个作业 A→B→A 各 2 次），
  purge 共 4 mm³，超出容量；
- **按材料交错**——两作业的 A 段合流、B 段合流、再回到 A 段——只需
  A→B、B→A 共 2 次换料，恰好不超容。

启动服务并调用：

```bash
docker compose up --build -d

curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d @examples/request_interleave.json
```

实际响应（`plan` 共 50 段，此处中间从略，其余字段与实际输出一致）：

```json
{
  "status": "FEASIBLE",
  "optimal": false,
  "objective": {
    "total_purge_mm3": 2,
    "total_tardiness_seconds": 0,
    "purge_optimal": true,
    "tardiness_optimal": true,
    "lexicographic_minimal": false
  },
  "plan": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "A",
     "print_seconds": 10, "volume_mm3": 5, "start_seconds": 0, "end_seconds": 10},
    {"order": 2, "job_id": "J1", "segment_index": 2, "material_id": "A",
     "print_seconds": 10, "volume_mm3": 5, "start_seconds": 10, "end_seconds": 20},
    ...
    {"order": 49, "job_id": "J2", "segment_index": 24, "material_id": "A",
     "print_seconds": 10, "volume_mm3": 5, "start_seconds": 484, "end_seconds": 494},
    {"order": 50, "job_id": "J2", "segment_index": 25, "material_id": "A",
     "print_seconds": 10, "volume_mm3": 5, "start_seconds": 494, "end_seconds": 504}
  ],
  "switches": [
    {"order": 1, "from_material": "A", "to_material": "B", "purge_mm3": 1,
     "switch_seconds": 2, "after_segment_order": 18, "at_seconds": 180},
    {"order": 2, "from_material": "B", "to_material": "A", "purge_mm3": 1,
     "switch_seconds": 2, "after_segment_order": 34, "at_seconds": 342}
  ],
  "job_tardiness": [
    {"job_id": "J1", "completion_seconds": 424, "deadline_seconds": 100000, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 504, "deadline_seconds": 100000, "tardy_seconds": 0}
  ],
  "purge_used_mm3": 2,
  "purge_remaining_mm3": 0,
  "total_tardiness_seconds": 0,
  "makespan_seconds": 504,
  "infeasible_reason": null,
  "solver_message": "purge 与逾期均已证最优；字典序决胜在限时内未完全证明，返回当前最优目标值下的可行计划（不声称字典序最小）"
}
```

结果解读：计划把两个作业的同材料连续段合并相邻——第 1～18 段为两作业的
A 段、第 19～34 段为 B 段、第 35～50 段为 A 段——仅 2 次换料
（A→B、B→A 各 1 mm³），purge 余量 0，两作业均按期完成。purge 与逾期
已证最优；50 个位置的字典序决胜未在限时内全部证明，故 `status` 为
`FEASIBLE` 而非 `OPTIMAL`（若证明完成则返回 `OPTIMAL`，计划内容不变）。

用极短时限（如把请求中 `"time_limit_seconds"` 改为 `0.5`）调用同一请求时，
CP-SAT 可能来不及给出任何解，服务改用免求解的构造性候选兜底：整作业串行
候选 purge 为 4、全部超容被弃，按材料交错候选 purge 为 2 被选中，仍返回
与上面相同的完整计划，`status` 为 `FEASIBLE` 且三个 `*_optimal` 标记均为
`false`——不会误报 `UNKNOWN` 和空计划。

### `POST /executions/audit`（执行核验）

提交与 `POST /plans` **完全相同的规划参数**，另外加一个按**实际执行顺序**排列的
`executions` 数组。服务先经 `PlanRequest` 校验、再用 `build_instance` 构建基准实例，
随后按实测记录**逐段回放**——不做紧密重算：实测起止时刻、段间空闲、实测打印耗时
全部原样保留，只检查执行过程是否合规。

新增请求字段（`executions[]` 必填，允许空数组 `[]` 表示尚未开工）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `executions[].job_id` | 非空字符串 | 已完成层段所属作业 ID（必须存在于 `jobs`） |
| `executions[].segment_index` | 正整数 | 作业内层段序号，从 1 开始（必须存在于该作业） |
| `executions[].start_seconds` | 非负整数 | 实测开始时刻（相对开工时刻 0，秒） |
| `executions[].end_seconds` | 非负整数 | 实测结束时刻（不得小于开始时刻） |
| `executions[].purge_before_mm3` | 非负整数 | 该层段开始前**实测**排出的 purge 体积（mm³） |

回放规则（逐段执行，材料以上一条已完成层段为准；首段以 `initial_material` 为准）：

- **换料耗时**：材料变化时，本段开始与上一段结束之间必须留足 `switch_seconds`
  （首段从时刻 0 起算）；间隔可以更长，多出部分记为空闲 `idle_seconds`；
- **段前 purge**：换料时实测值不得低于 `purge_matrix[from][to]` 的需求，容量按
  **实测值**扣减；**同材料时段前 purge 必须为 0**（同材料本就不该换料 / 排料）；
- **跨作业可交错**，但每个作业的层序必须严格按 1、2、3… 连续递增（不得跳段、乱序）；
- 后一条记录的开始不得早于上一条已完成层段的结束（时间不得重叠）；
- 实测打印耗时与请求中声明的 `print_seconds` 允许不同——以实测为准，不判违规；
- 仅当某作业**全部层段**都出现在记录中时才算完工，才按其最后层段实测结束时刻
  核算逾期；**未完工前缀不计未完工作业逾期**。

响应 `status`：

| `status` | 条件 |
| --- | --- |
| `COMPLETED` | 全部层段均已提交（数量等于计划层段总数）且无任何违规 |
| `IN_PROGRESS` | 尚无违规，但层段未交齐（含 `executions` 为空数组） |
| `VIOLATED` | 回放中发现至少一处违规；即使层段交齐也**不能**标记完成 |

违规代码（逐条给出 `execution_order` / `job_id` / `segment_index` / `message` / 实测值 / 要求值）：

| `violations[].code` | 含义 |
| --- | --- |
| `OVERLAP` | 实测开始早于上一已完成层段结束，时间重叠 |
| `LAYER_ORDER` | 作业内层序非法（跳段 / 乱序；跨作业交错本身合法） |
| `SWITCH_TIME` | 换料间隔不足 `switch_seconds` |
| `SAME_MATERIAL_PURGE` | 同材料相邻但段前实测 purge 非零（必须为 0） |
| `PURGE_INSUFFICIENT` | 换料段前实测 purge 低于矩阵需求 |
| `PURGE_OVER_CAPACITY` | 段前实测 purge 累计超过总容量（`purge_remaining_mm3` 为负） |

#### 核验示例 1：合规完成（`examples/audit_completed.json`）

请求体 = `examples/request.json` 的规划参数 + `executions`（实测与最优计划一致）：

```json
"executions": [
  {"job_id": "J1", "segment_index": 1, "start_seconds": 0,   "end_seconds": 60,  "purge_before_mm3": 0},
  {"job_id": "J1", "segment_index": 2, "start_seconds": 70,  "end_seconds": 100, "purge_before_mm3": 120},
  {"job_id": "J2", "segment_index": 1, "start_seconds": 100, "end_seconds": 180, "purge_before_mm3": 0},
  {"job_id": "J2", "segment_index": 2, "start_seconds": 190, "end_seconds": 210, "purge_before_mm3": 80}
]
```

启动服务并调用（Docker Compose 一键启动见上一节；本地直跑监听同一端口）：

```bash
docker compose up --build -d

curl -X POST http://localhost:8000/executions/audit \
  -H 'Content-Type: application/json' \
  -d @examples/audit_completed.json
```

**实际响应**：

```json
{
  "status": "COMPLETED",
  "executed_segment_count": 4,
  "total_segment_count": 4,
  "replay": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "PLA",
     "start_seconds": 0, "end_seconds": 60,
     "actual_print_seconds": 60, "planned_print_seconds": 60,
     "purge_before_mm3": 0, "required_purge_mm3": 0,
     "switched": false, "switched_from_material": null,
     "gap_seconds": 0, "required_switch_seconds": 0, "idle_seconds": 0},
    {"order": 2, "job_id": "J1", "segment_index": 2, "material_id": "ABS",
     "start_seconds": 70, "end_seconds": 100,
     "actual_print_seconds": 30, "planned_print_seconds": 30,
     "purge_before_mm3": 120, "required_purge_mm3": 120,
     "switched": true, "switched_from_material": "PLA",
     "gap_seconds": 10, "required_switch_seconds": 10, "idle_seconds": 0},
    {"order": 3, "job_id": "J2", "segment_index": 1, "material_id": "ABS",
     "start_seconds": 100, "end_seconds": 180,
     "actual_print_seconds": 80, "planned_print_seconds": 80,
     "purge_before_mm3": 0, "required_purge_mm3": 0,
     "switched": false, "switched_from_material": null,
     "gap_seconds": 0, "required_switch_seconds": 0, "idle_seconds": 0},
    {"order": 4, "job_id": "J2", "segment_index": 2, "material_id": "PLA",
     "start_seconds": 190, "end_seconds": 210,
     "actual_print_seconds": 20, "planned_print_seconds": 20,
     "purge_before_mm3": 80, "required_purge_mm3": 80,
     "switched": true, "switched_from_material": "ABS",
     "gap_seconds": 10, "required_switch_seconds": 10, "idle_seconds": 0}
  ],
  "switches": [
    {"order": 1, "from_material": "PLA", "to_material": "ABS",
     "after_execution_order": 1, "at_seconds": 60, "next_start_seconds": 70,
     "gap_seconds": 10, "required_switch_seconds": 10,
     "purge_before_mm3": 120, "required_purge_mm3": 120},
    {"order": 2, "from_material": "ABS", "to_material": "PLA",
     "after_execution_order": 3, "at_seconds": 180, "next_start_seconds": 190,
     "gap_seconds": 10, "required_switch_seconds": 10,
     "purge_before_mm3": 80, "required_purge_mm3": 80}
  ],
  "violations": [],
  "violation_count": 0,
  "purge_used_mm3": 200,
  "purge_remaining_mm3": 200,
  "completed_job_tardiness": [
    {"job_id": "J1", "completion_seconds": 100, "deadline_seconds": 120, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 210, "deadline_seconds": 220, "tardy_seconds": 0}
  ],
  "total_tardiness_seconds": 0,
  "makespan_seconds": 210
}
```

要点：两次换料（PLA→ABS 实测 120、ABS→PLA 实测 80）都恰好满足矩阵需求且留足
10s，同材料相邻的 J2.1 段前 purge 为 0；两作业均按期完工，`COMPLETED`。

#### 核验示例 2：进行中，保留空闲与实测耗时（`examples/audit_in_progress.json`）

只完成了 J1.1 与 J2.1，且实测耗时与计划不同（62s vs 60s、83s vs 80s）：

```json
"executions": [
  {"job_id": "J1", "segment_index": 1, "start_seconds": 0,  "end_seconds": 62,  "purge_before_mm3": 0},
  {"job_id": "J2", "segment_index": 1, "start_seconds": 72,  "end_seconds": 155, "purge_before_mm3": 120}
]
```

```bash
curl -X POST http://localhost:8000/executions/audit \
  -H 'Content-Type: application/json' \
  -d @examples/audit_in_progress.json
```

**实际响应**（节选）：

```json
{
  "status": "IN_PROGRESS",
  "executed_segment_count": 2,
  "total_segment_count": 4,
  "replay": [
    {"order": 1, "job_id": "J1", "segment_index": 1, "material_id": "PLA",
     "start_seconds": 0, "end_seconds": 62,
     "actual_print_seconds": 62, "planned_print_seconds": 60,
     "purge_before_mm3": 0, "required_purge_mm3": 0,
     "switched": false, "switched_from_material": null,
     "gap_seconds": 0, "required_switch_seconds": 0, "idle_seconds": 0},
    {"order": 2, "job_id": "J2", "segment_index": 1, "material_id": "ABS",
     "start_seconds": 72, "end_seconds": 155,
     "actual_print_seconds": 83, "planned_print_seconds": 80,
     "purge_before_mm3": 120, "required_purge_mm3": 120,
     "switched": true, "switched_from_material": "PLA",
     "gap_seconds": 10, "required_switch_seconds": 10, "idle_seconds": 0}
  ],
  "switches": [
    {"order": 1, "from_material": "PLA", "to_material": "ABS",
     "after_execution_order": 1, "at_seconds": 62, "next_start_seconds": 72,
     "gap_seconds": 10, "required_switch_seconds": 10,
     "purge_before_mm3": 120, "required_purge_mm3": 120}
  ],
  "violations": [],
  "violation_count": 0,
  "purge_used_mm3": 120,
  "purge_remaining_mm3": 280,
  "completed_job_tardiness": [],
  "total_tardiness_seconds": 0,
  "makespan_seconds": 155
}
```

要点：`actual_print_seconds` 以实测为准（与 `planned_print_seconds` 并列给出），
`makespan_seconds` 取实测末段结束时刻 155；J1、J2 都未交齐全部层段，
`completed_job_tardiness` 为空、逾期不计（即使实测时刻已超过某截止值）。
换料间隔若长于 `switch_seconds`，差额会计入该段的 `idle_seconds` 并原样保留。

#### 核验示例 3：多处违规，不能标记完成（`examples/audit_violations.json`）

容量改为 150 mm³，四条层段交齐但执行过程问题很多：J1.1 同材料却排了 5 mm³ purge；
J1.2 换料只留 5s（需 10s）且 purge 仅 100（需 120）；J2.1 与上一段时间重叠；
J2.2 换料只留 5s；累计实测 purge 185 超出容量 150：

```json
"switch_seconds": 10,
"total_purge_capacity_mm3": 150,
"executions": [
  {"job_id": "J1", "segment_index": 1, "start_seconds": 0,   "end_seconds": 60,  "purge_before_mm3": 5},
  {"job_id": "J1", "segment_index": 2, "start_seconds": 65,  "end_seconds": 95,  "purge_before_mm3": 100},
  {"job_id": "J2", "segment_index": 1, "start_seconds": 90,  "end_seconds": 170, "purge_before_mm3": 0},
  {"job_id": "J2", "segment_index": 2, "start_seconds": 175, "end_seconds": 195, "purge_before_mm3": 80}
]
```

```bash
curl -X POST http://localhost:8000/executions/audit \
  -H 'Content-Type: application/json' \
  -d @examples/audit_violations.json
```

**实际响应**（`replay` 与 `switches` 字段同上例结构，此处只列关键部分）：

```json
{
  "status": "VIOLATED",
  "executed_segment_count": 4,
  "total_segment_count": 4,
  "violations": [
    {"code": "SAME_MATERIAL_PURGE", "execution_order": 1, "job_id": "J1", "segment_index": 1,
     "message": "本段材料 PLA 与上一段相同，同材料段前 purge 必须为 0，实测 5 mm^3",
     "actual": 5, "required": 0},
    {"code": "SWITCH_TIME", "execution_order": 2, "job_id": "J1", "segment_index": 2,
     "message": "材料由 PLA 换为 ABS，段前实测间隔仅 5s，不足 switch_seconds=10s",
     "actual": 5, "required": 10},
    {"code": "PURGE_INSUFFICIENT", "execution_order": 2, "job_id": "J1", "segment_index": 2,
     "message": "材料由 PLA 换为 ABS，段前实测 purge 100 mm^3 低于矩阵需求 120 mm^3",
     "actual": 100, "required": 120},
    {"code": "OVERLAP", "execution_order": 3, "job_id": "J2", "segment_index": 1,
     "message": "层段实测开始 90s 早于上一已完成层段结束 95s，时间重叠 5s",
     "actual": 90, "required": 95},
    {"code": "SWITCH_TIME", "execution_order": 4, "job_id": "J2", "segment_index": 2,
     "message": "材料由 ABS 换为 PLA，段前实测间隔仅 5s，不足 switch_seconds=10s",
     "actual": 5, "required": 10},
    {"code": "PURGE_OVER_CAPACITY", "execution_order": 4, "job_id": "J2", "segment_index": 2,
     "message": "段前 purge 后累计实测消耗 185 mm^3，超过总容量 150 mm^3",
     "actual": 185, "required": 150}
  ],
  "violation_count": 6,
  "purge_used_mm3": 185,
  "purge_remaining_mm3": -35,
  "completed_job_tardiness": [
    {"job_id": "J1", "completion_seconds": 95,  "deadline_seconds": 120, "tardy_seconds": 0},
    {"job_id": "J2", "completion_seconds": 195, "deadline_seconds": 220, "tardy_seconds": 0}
  ],
  "total_tardiness_seconds": 0,
  "makespan_seconds": 195
}
```

要点：四类问题按发生位置逐条累积（同一段可同时报多条，如第 2 条同时为换料耗时不足
与 purge 不足）；容量始终按**实测值**（5+100+0+80=185）扣减，余量如实给出负数 -35；
虽然四条层段都已交齐，但存在违规，状态是 `VIOLATED` 而**不是** `COMPLETED`。
全部层段出现的作业其逾期仍照常核算（本例均未逾期）；未完工前缀则不会出现在
`completed_job_tardiness` 中。

#### 核验接口的非法请求（HTTP 422）

`/executions/audit` 继承 `/plans` 的全部输入校验（作业数、矩阵完整性、非负等），
另外对每条实测记录做引用 / 重复 / 时序校验。以下情况返回 **422**，不进入回放：

- 引用了**未知作业**（`job_id` 不在 `jobs` 中）或**未知层段**（该作业没有此 `segment_index`）；
- **重复层段**：同一 `(job_id, segment_index)` 出现两次；
- **起止倒置**：`end_seconds < start_seconds`；
- 缺少 `executions` 字段（注意：字段必须存在；传空数组 `[]` 合法，返回 `IN_PROGRESS`）；
- 以及继承自 `/plans` 的全部 422 情形（作业数越界、`job_id` 重复、矩阵缺行缺列、
  负数、对角非 0、额外字段等）。

示例（重复层段）：

```bash
curl -i -X POST http://localhost:8000/executions/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "jobs": [
      {"job_id": "J1", "deadline_seconds": 120, "segments": [
        {"material_id": "PLA", "print_seconds": 60, "volume_mm3": 100},
        {"material_id": "ABS", "print_seconds": 30, "volume_mm3": 40}]},
      {"job_id": "J2", "deadline_seconds": 220, "segments": [
        {"material_id": "ABS", "print_seconds": 80, "volume_mm3": 120},
        {"material_id": "PLA", "print_seconds": 20, "volume_mm3": 30}]}
    ],
    "initial_material": "PLA",
    "purge_matrix": {"PLA": {"PLA": 0, "ABS": 120}, "ABS": {"PLA": 80, "ABS": 0}},
    "switch_seconds": 10,
    "total_purge_capacity_mm3": 400,
    "executions": [
      {"job_id": "J1", "segment_index": 1, "start_seconds": 0,  "end_seconds": 60, "purge_before_mm3": 0},
      {"job_id": "J1", "segment_index": 1, "start_seconds": 60, "end_seconds": 90, "purge_before_mm3": 0}
    ]
  }'
# HTTP/1.1 422 Unprocessable Entity
# ... "msg": "Value error, executions 第 2 条为重复层段: J1.1" ...
```

### 求解状态语义

| `status` | `optimal` | 含义 |
| --- | --- | --- |
| `OPTIMAL` | `true` | 三级目标（含字典序决胜）均已由 CP-SAT 证明最优 |
| `FEASIBLE` | `false` | 已得到可行计划并给出完整明细，但**未证明全部目标最优**；`objective` 中三个 `*_optimal` 标记分别如实表示 purge / 逾期 / 字典序是否确已证明（含 CP-SAT 超时后由构造性排列兜底的情形，此时三者均为 `false`） |
| `INFEASIBLE` | `false` | 无解，见 `infeasible_reason` |
| `UNKNOWN` | `false` | 限时内连可行计划都无法给出（构造性候选也不满足容量） |

### 非法数据（HTTP 422）

以下情况返回 **422 Unprocessable Entity**（FastAPI/Pydantic 标准错误体，含字段定位）：

- 作业数不在 2～8；`job_id` 重复或为空；
- 层段列表为空；打印秒数 / 体积 / 截止秒数 / 切换耗时 / 容量为负；
- purge 矩阵单元格为负、对角非 0；
- 初始材料或某层段材料在矩阵中缺行或缺列；
- 出现未声明的额外字段、类型错误等。

示例：

```bash
curl -i -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{"jobs":[],"initial_material":"PLA",
       "purge_matrix":{"PLA":{"PLA":0}},
       "switch_seconds":10,"total_purge_capacity_mm3":100}'
# HTTP/1.1 422 Unprocessable Entity
```

---

## 四、项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 应用：GET /health、POST /plans、POST /executions/audit
│   ├── schemas.py     # 请求/响应模型与 422 输入校验（含执行核验模型）
│   ├── solver.py      # CP-SAT 建模、分阶段求解与字典序决胜；build_instance 基准构建
│   └── audit.py       # /executions/audit 逐段回放核验（保留空闲与实测耗时，不重排）
├── examples/
│   ├── request.json              # 可行示例（/plans）
│   ├── request_long_job.json     # 长作业示例（单作业 25 段）
│   ├── request_interleave.json   # 按材料交错示例（两个 25 段 A→B→A 作业，容量 2）
│   ├── request_infeasible.json   # 容量超限示例
│   ├── audit_completed.json      # 执行核验：合规完成示例
│   ├── audit_in_progress.json    # 执行核验：进行中前缀（保留空闲/实测耗时）示例
│   └── audit_violations.json     # 执行核验：多处违规示例
├── tests/
│   ├── test_plans.py  # 16 个端到端测试（最优性、无解、交错、优先级、长作业、超时兜底、422 等）
│   └── test_audit.py  # 19 个执行核验测试（三态、空闲/实测保留、层序/重叠/换料/purge、逾期前缀、422 等）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 五、建模与求解说明

- 每个层段为不可拆分节点，另设虚拟起/止节点；节点间有向弧表示“紧随其后执行”，
  弧上携带 purge 体积与切换耗时；`AddCircuit` 强制选出一条覆盖全部层段的
  Hamilton 回路（即合法排列）。
- 作业内层段用先后约束限制；弧被选中时强制层段开始时刻 = 上一层段结束 +
  切换耗时，因此计划天然紧密无空转。
- 求解分三阶段：① CP-SAT 最小化总 purge（容量为硬约束）；② 固定 purge 最优值
  后最小化总逾期；③ **逐位贪心 + 可行性证明**：对执行序列的每个位置，按
  `(作业 ID, 层序)` 升序逐一尝试固定候选层段并用 CP-SAT 验证可行性，第一个可行者
  即为该位字典序最优选择；每一步都经过可行性证明，全部位置完成即字典序最小。
  任何一步在限时内无法判定，则返回当前最优目标值下的可行计划并保守地标为
  `FEASIBLE`，绝不声称未证明的最优性。
- 无解时临时移除容量上限再求最小 purge：仍无解归因为 `PRECEDENCE`，
  最小 purge 超出容量则归因为 `CAPACITY` 并给出最小需求值。
- 层段数没有固定上限，长作业直接建模；大模型在 `time_limit_seconds` 内
  未求得可行解时，用免求解的构造性排列兜底：整作业串行（输入顺序、反向、
  截止时间升序、作业 ID 升序）之外，还把每个作业切成同材料连续段（run）
  并按材料交错——"同材料连续打印优先"与"截止时间优先"两种贪心在少换料与
  少逾期之间取舍。全部候选严格遵守作业内层序、精确核算 purge 后仅取不超
  容者，并按（purge、逾期、字典序）择优，仍返回完整、按序的计划与全部
  明细，状态如实标为 `FEASIBLE`，仅当兜底排列也超容时才返回 `UNKNOWN`。
  三级目标的最优性标记互相独立，只有 CP-SAT 对相应目标（以及字典序决胜
  的每一步可行性）确已证明时才置为 `true`。

### 执行核验（`/executions/audit`）如何回放

- 核验复用 `build_instance` 构建与 `/plans` 相同的基准实例（材料、矩阵、容量、
  层序关系完全一致），但**不调用 CP-SAT、不重排时间线**：层段严格按
  `executions` 给出的实际执行顺序逐段回放，实测起止、段间空闲与实测打印耗时
  原样保留（回放结果同时给出 `actual_print_seconds` 与 `planned_print_seconds`
  便于对照，二者不同不判违规）。
- 每段的“上一材料”取上一条已完成层段的实测材料（首段取 `initial_material`）：
  材料变化才允许排 purge（且不得低于矩阵需求、间隔不得短于 `switch_seconds`），
  同材料段前 purge 必须为 0；容量按**段前实测值**逐段累加扣减，超容即记
  `PURGE_OVER_CAPACITY`，余量为负时如实返回。
- 层序合法性按**作业分别**维护“下一个应出现的层序”：跨作业可任意交错，
  但每个作业只能连续递增地出现 1、2、3…；时间重叠按实际执行顺序逐对比较
  （后段开始不得早于前段结束）。违规会逐段累积、互不短路，因此一次核验可
  返回多个位置的多处问题；存在任何违规即 `VIOLATED`，层段交齐也不标完成。
- 逾期只对**全部层段均已出现**的作业、按其最后层段实测结束时刻核算；
  未完工前缀的作业不进入 `completed_job_tardiness`。
