# Apiary

Apiary 是一个基于 Python + Tkinter + SQLite + Ollama 的无人机任务调度与仿真项目，用于模拟多机协同、任务分配、局部避障和信号场观测。它既适合作为桌面演示工具，也能作为无人机任务调度逻辑的原型实验平台。

这个项目的核心目标是：

- 使用任务优先级与队列管理处理多任务请求
- 在运行时维护无人机状态和局部信号观测
- 支持 AI 参与任务解析、宏观分配和局部路线建议（与本地分配规则的边界见下文）
- 底层移动由仿真规则执行，并对紫色危险落点进行拦截
- 将任务持久化到 SQLite；每次启动重新初始化演示任务，飞行状态和多数运行参数仅保存在内存

## 当前实现重点

当前代码已经把以下几个关键点落地：

- 任务统一存储到数据库，每次启动重新初始化演示任务
- 多机任务拆为等权子任务，每完成一个累计 `1/N` 进度；抢占保留已完成部分，未完成部分重新执行
- 运输任务按“起点 → 终点”执行
- 本地与 AI 分配共用配额和抢占约束，并检查异步 AI 结果是否过期
- 任务变化时同步数据库，调度 FIFO 与界面展示排序分离
- 任务优先级统一为 `urgent / high / normal / low`
- AI 包括任务解析、宏观分配和局部路线重规划三个入口
- 局部 AI 路线写入 `planned_route` 后，不再叠加本地 A* 或编队偏移
- 到达局部 AI 航点后，系统会继续执行后续真实目标，不会提前结束任务
- 运行时会记录调度原因、AI 调度次数、热力图版本和任务状态变化
- GUI 中展示无人机状态、地图、任务池、事件日志和可视化流程

## 项目结构

```text
Apiary/
├── AI/
│   ├── agent_service.py      # AgentService：自然语言任务解析、调度策略和局部路线重规划
│   ├── ollama_client.py      # Ollama /api/generate HTTP 客户端、JSON/schema 输出和失败降级
│   ├── prompts.py            # 三类 AI 提示词
│   ├── schema.py             # AI 返回值的轻量 schema 校验
│   └── __init__.py
├── core/
│   ├── backend.py            # ApiaryBackend：任务入口、事件驱动调度、AI 协调、数据库同步
│   ├── simulation.py         # SimulationEngine：无人机状态、观测、移动、避障、编队和抵达判定
│   ├── task_pool.py          # Task/TaskPool：任务模型、优先级、状态和多机配额
│   ├── taskdb.py             # TaskDB：task_data.db 的 SQLite 持久化与迁移
│   ├── scheduler.py          # AutoScheduler：后台线程按间隔调用 backend.tick_simulation()
│   ├── pathplan.py           # 基于局部热力图的 A* 区块路径规划
│   ├── zones.py              # 绿色/黄色/红色/紫色信号分区的唯一事实来源
│   ├── credentials.py        # 管理员/客户端用户共用的密码哈希、识别和校验
│   └── __init__.py
├── GUI/
│   ├── main.py               # Tk 启动入口，创建数据库、后端和调度线程
│   ├── loginpage.py          # 管理员登录
│   ├── mainpage.py           # 登录后的导航容器
│   ├── drone_page.py         # 无人机、地图、任务和事件监控
│   ├── request_page.py       # 自然语言任务提交、任务表和优先级修改
│   ├── visualization_page.py # 生命周期/架构/避障状态可视化
│   ├── settings_page.py      # 运行设置和状态展示
│   ├── user_page.py          # 客户端用户 CRUD 管理
│   ├── passworddb.py         # GUI/password.db 的管理员账户存储
│   ├── clientuserdb.py       # GUI/client_users.db 的客户端用户存储
│   └── __init__.py
├── tests/test_backend_scheduler.py # 后端、调度、仿真和 AI 兜底回归测试
├── _probe_sep*.py             # 多机编队/间距的隔离探针，不是运行入口
└── README.md
```

运行时生成的数据库文件不应视为源码：`task_data.db` 保存任务与设置；`GUI/password.db` 保存管理员登录账户；`GUI/client_users.db` 保存客户端用户，供网页提交任务时验证账号密码。

## 代码调用关系

### 1. 启动与页面

```text
GUI/main.py → App
  ├─ UserDB → GUI/password.db → credentials（管理员密码哈希/验证）
  ├─ ClientUserDB → GUI/client_users.db（客户端用户管理）
  ├─ ApiaryBackend
  │   ├─ TaskPool + SimulationEngine + AgentService + TaskDB
  │   └─ 加载/筛选历史任务 → 补默认任务 → 同步数据库 → 配置运行时
  ├─ AutoScheduler.start() → 后台 _run() → backend.tick_simulation()
  ├─ LoginPage → UserDB.verify_user() → 登录成功显示 MainPage
  └─ MainPage → DronePage / RequestPage / VisualizationPage / UserPage / SettingsPage
```

五个业务页面在启动时一次性创建，默认业务页为无人机监控；**调度线程在管理员登录后启动**，返回登录页时停止。退出应用会停止网页服务和调度器，关闭任务提交线程池及后端。

| 页面 | 主要调用 | 职责与边界 |
|---|---|---|
| RequestPage | `list_tasks()`、`submit_user_request()`、`update_task_priority()` | 提交请求、查看任务、显式覆盖优先级/机数 |
| DronePage | `configure_runtime()`、`tick_simulation(force_ai=False)`、`get_pipeline_state()`、`get_event_trace()`、仿真状态查询 | 周期读取状态、绘图、手动运行一轮；不实现路径算法 |
| VisualizationPage | `task_pool.get_sorted_tasks()`、`simulation.get_drones_state()`、后端计数和热力图版本 | 展示架构和实时生命周期，不是另一套调度器 |
| SettingsPage | 运行时/观测摘要查询 | 展示运行设置；已移除启动重置开关；“断点测试”只显示提示框 |
| UserPage | `ClientUserDB.list_users/create_user/update_user/delete_user()` | 管理网页提交使用的客户端账号，与管理员登录库分离 |

### 2. 自然语言任务入口

```text
RequestPage.submit_task()
  → ApiaryBackend.submit_user_request()
    → AgentService.parse_user_task()
      ├─ 本地解析坐标/A–E点/基地、起终点、显式机数
      ├─ USER_TASK_PROMPT + USER_TASK_SCHEMA → OllamaClient.call()
      ├─ 清洗模型字段 → 本地地点/机数兜底 → 校验
      └─ 本地生成任务号；无可识别终点则返回 rejected
    → 显式 priority/required_drones/all_drones 覆盖模型结果
    → 与任务池及数据库比对编号，重复则追加后缀
    → _make_task() → TaskPool.add_task() + TaskDB.add_or_update_task()
    → mark_requests_dirty() + record_event()
```

- 任务等级为 `urgent / high / normal / low`；身份和用户等级传给 AI 并留痕，不直接参与本地排序。
- 本地“所有无人机”“派三架”等明确机数不会被模型降为单机，配额受当前机队规模限制。
- 地点解析支持坐标、A–E 任务点和基地别名；本地解析坐标会夹取到沙盘范围 `[-220, 220]`。
- 自然语言入口仅接受 `new_task / transport / recon`，其他 action 会被拒绝；取消任务与修改优先级使用独立后端接口，不应将 schema 枚举视为已支持的自然语言控制能力。

### 3. 一个调度 tick 的实际顺序

`AutoScheduler._run()` 持续调用 `ApiaryBackend.tick_simulation()`。后者用 `RLock` 串行保护 tick，进入 `_tick_simulation_locked()`：

```text
SimulationEngine.tick()：观测 → 移动/返航/待命
  → _settle_tasks()：抵达结算、解绑待命机
  → 收取已完成的策略 AI / 路线 AI Future
  → 应用有效局部路线，并为受阻无人机提交路线重规划
  → drain_reschedule_reason()：判断是否重新调度
  → _assign_tasks()：本地分配优先
  → 必要时异步请求 AI 策略，或应用已返回的 AI assignments
  → 必要时再做本地补充分配
  → 更新流程/事件 → TaskDB.flush_tasks() → 返回快照
```

这意味着新绑定任务通常在后续 tick 才开始移动，而不是“先分配再移动”。

重新调度触发条件包括请求变化、任务状态变化，以及 **`queue_drain`：即使指纹没有变化，只要仍有可补派的配额和可用无人机，也会补充分配**。所以不能简单描述为“只有请求/状态变化才会调度”。

本地 `_assign_tasks()` 的规则：

- 跳过已完成、已取消任务；按任务池返回顺序处理，按需求台数扣除当前持有者数量。
- 非紧急任务只选无任务且不在返航的无人机；紧急任务还可抢占低于 urgent 的任务持有者。
- 选择代价为“当前负载 → 到起点的直线距离 → A* 路程/直线距离比 → 无人机编号”。
- 抢占通过 `_preempt()` 把旧任务退回 pending，并清空旧路线；绑定通过 `SimulationEngine.bind_task()` 完成。
- 调度使用独立的优先级/FIFO 顺序，同等级按提交顺序处理；界面展示可单独排序，不改变调度顺序。
- 抢占后已完成子任务保留，未完成子任务退回待分配，并在重新绑定后从任务起点执行。

### 4. AI 的三个入口与接管边界

| AgentService 方法 | 调用方与触发条件 | 返回结果的消费者 |
|---|---|---|
| `parse_user_task()` | `submit_user_request()`，提交自然语言时同步调用 | 任务创建入口 |
| `parse_simulation_strategy()` | 后端策略线程池；`force_ai=True` 或事件触发后本地未分配出新任务，且 AI 开启、无在途策略请求 | `_apply_ai_assignments()` → `bind_task()` |
| `parse_route_replan()` | 后端路线线程池；无人机有任务且处于 planning/blocked/holding，未在请求中且不在冷却期 | `apply_ai_route()` → `planned_route` |

策略与路线各使用一个单工作线程池，tick 轮询 Future 而不是等待模型响应。任务解析接口本身同步，但 GUI 已通过独立线程池提交并轮询结果，解析等待不会阻塞界面；网页也在 HTTP 工作线程中调用，不操作 Tk 控件。

AI 策略的 `assignments` 与本地分配共用配额及抢占约束：非紧急任务不抢占在飞任务，紧急任务只能抢占允许的低等级任务。异步策略和路线结果应用前会检查是否过期；模型建议不能绕过任务终结状态和机数约束。

所有入口均经 `OllamaClient.call()` → `/api/generate`，使用对应 prompt/schema。默认地址为 `http://localhost:11434`，模型为 `qwen3:8b`。不支持 schema 输出时尝试 JSON 模式；超时不继续重试。普通调用默认超时 60 秒，路线调用为 30 秒、一次尝试。模型配置在客户端构造参数中，没有 README 所暗示的通用配置加载机制。

`schema.py` 是轻量必填字段/类型/枚举校验，不是完整 JSON Schema 验证器；路线坐标的形状和范围还需由仿真层检查。

### 5. 路线、观测与避障

```text
SimulationEngine.tick()
  ├─ _observe() → SignalField + zones → 局部观测/observed_heatmap
  ├─ 无任务或已抵达：待命
  ├─ returning_to_base：_return_to_base() → _apply_step()
  └─ _advance()
      ├─ 非 AI 路线：ensure_route() → pathplan.plan_route() → _lane_route()
      ├─ AI 路线：按原航点推进，不重新叠加 A* 或编队偏移
      ├─ 信号判断/停滞检测/回退/侧滑 → _apply_step()
      └─ 抵达：_finish_leg() → 后端下一次结算
```

- `SignalField` 生成确定性的仿真信号场；规划使用逐步累计的本地观测热力图，不等于模型直接读取全局真值。
- `zones.py` 统一阈值：绿色 ≥80，黄色 ≥55 且 <80，红色 ≥30 且 <55，紫色 <30。未探测区单独显示为中性色。
- `pathplan.py` 在 18 单位区块的八邻域做 A*，绿色/黄色/未知/红色代价分别为 1/1.8/2/20，紫色一般不可通行；**终点区块有特殊放行分支**。无路或绕路超过直线距离 5 倍时返回直达目标航点，之后仍需移动层检查。
- `_apply_step()` 检查实际落点是否为紫色，拒绝危险落点；红区停滞可回退、重规划或降级限速直行。该检查不是连续线段碰撞检测，不能宣称整段轨迹已有严格安全证明。
- 紧急任务可借用附近 3×3 区块内非紫色信号；底层紫色落点拦截不会因此取消。
- `bind_task()` 为同任务多机分配目标偏移和共轴飞行道，减少重叠，但不是完备的多机碰撞规避系统。
- `apply_ai_route()` 要求至少两个二维、范围内航点，原样写入 `planned_route` 并设置 `ai_route_active`，不是“仅一个短距离航点”；没有在接收时执行完整路线安全验证。
- AI 局部路线结束而真实任务终点尚未抵达时，`_finish_leg()` 清理局部路线并恢复本地规划，不提前完成任务。AI 路线受阻时可等待下一次 AI 重规划。
- 路线请求附带位置、真实目标、步长、失败方向、其他无人机及最多 150 个邻近观测块；应用结果时核对当前任务号，不是对所有历史状态做完整一致性校验。

`SimulationEngine.apply_strategy()` 是另一个可单独调用的策略接口，测试会直接覆盖它；后端主 tick 的 AI 分配使用 `_apply_ai_assignments()`，不是调用该接口，二者限制不能混为一谈。

### 6. 任务完成与数据库

多机任务拆为等权子任务，单个子任务完成增加父任务 `1/N` 进度；全部子任务完成后父任务才完成。已完成子任务保留，未完成部分被抢占后重新待分配，并从任务起点重新执行。无人机完成后按任务配置待命或返航，后端结算并解绑以便复用。

| 数据 | 实际存储 |
|---|---|
| 任务内容、起终点、等级、任务状态、需求机数、身份留痕 | `task_data.db` 的 tasks 表；每 tick 批量同步 |
| 启动重置开关 `reset_tasks_on_startup` | 同库 settings 表 |
| 速度、机队数、动态任务数量、AI 开关 | 后端内存 runtime；初始化重新设置，不从 settings 恢复 |
| 无人机坐标、路线、局部热力图、AI Future、事件日志 | 内存，不提供完整运行状态断点恢复 |
| 管理员登录 / 客户端用户 | 分别由两个 GUI 数据库维护 |

`TaskDB` 复用 SQLite 连接、使用锁与 WAL，并为旧库补可选列。任务变化时才同步持久化，避免无变化 tick 的重复写入。每次启动均清空任务数据并初始化新演示，不恢复上次运行进度；旧的 `reset_tasks_on_startup` 设置不再控制启动行为，GUI 已移除该开关。

初始化还会调用 `configure_runtime(task_count=len(task_pool.tasks))` 创建 `task_generated_*` 动态任务，默认任务数不是固定四条；动态任务数量设置也不是整个任务池总数上限。

## 网页任务提交（本机 3000 端口）

启动桌面端后会同时启动一个仅用于提交任务的网页服务：

```text
http://127.0.0.1:3000
```

同一局域网内的其他设备可使用运行 Apiary 电脑的局域网 IP 访问，例如 `http://192.168.1.10:3000`。网页提交的是**任务描述**，不是文件上传；提交前会使用 `GUI/client_users.db` 中的客户端账号和密码验证身份，验证通过后任务会进入桌面端的同一个任务池。客户端账号由 GUI 的“用户管理”页面创建和维护，演示账号为 `client001 / 123456`（请勿用于公开环境）。

网页服务在管理员登录桌面端后才接受任务，返回登录页或退出程序时会暂停/关闭提交；网页任务提交不会直接操作 Tkinter 控件。服务默认监听 `0.0.0.0:3000`，因此需要在 Windows 防火墙中允许 Python 接受局域网连接，并只应在可信局域网使用。当前使用 HTTP，账号密码会在局域网中明文传输，生产环境应放在 HTTPS 反向代理后。请求仅接受 JSON、单次任务最多 8000 字，并限制并发提交；模型解析期间网页会等待结果，网络中断时请先在桌面端确认任务状态再重试。

## 运行方式

以下“项目目录”指包含本说明以及 AI、core、GUI 的内层 Apiary 目录。

代码使用 Python 3.10+ 类型语法，建议使用正常可用的 Python 3.12，并确保包含 Tkinter 和 SQLite 标准库。业务代码没有必需的第三方 Python 运行依赖；测试需要 pytest。Ollama 是独立服务，不是 pip 包。

在项目目录的 PowerShell 中执行：

```powershell
py -3.12 .\GUI\main.py
```

若已有可用虚拟环境，也可使用其解释器运行同一入口。仓库附带的 `.venv` 可能依赖原机器的 Python 安装路径，不保证复制后可用。

需要 AI 时，另行启动本地 Ollama 服务并准备 `qwen3:8b` 模型。没有 Ollama 时，明确地点/机数可本地兜底建单，本地调度与移动仍可执行，但 AI 的语义判断和路线/策略建议不可用；不要将其视为完整离线 AI 等价模式。

管理员登录库与客户端用户库分别初始化演示账户；已有数据库以自身内容为准。客户端用户不能直接作为管理员登录。演示账户不适合在公开或生产环境使用。

## 测试与探针

```powershell
py -3.12 -m pytest .\tests\test_backend_scheduler.py -q
```

测试使用 FakeAI/FakeTaskDB、内存 SQLite 或临时数据库，覆盖事件驱动调度、优先级与抢占、多机配额、坐标解析、任务完成/返航、信号分区、A*、热力图及任务解析兜底等。测试通过不代表真实 Ollama、GUI 交互或飞行安全已经验证。

`_probe_sep.py` 至 `_probe_sep7.py` 是多机重叠、飞行道、起飞窗口及间距探针；部分包含历史机器绝对路径，运行前需检查。它们不属于 pytest 自动发现的正式测试，也不由 GUI 启动链调用。

## 已知边界

- 本项目是桌面仿真原型，不对接真实无人机控制器，也不提供生产级安全保证。
- AI 宏观分配与本地分配的约束尚未完全统一；自然语言 action 枚举不等于完整控制命令实现。
- 任务起点会参与分配代价和编队基准，但当前 `ensure_route()` 从无人机当前位置直接规划到任务终点，没有强制先到 from_location 再到 to_location 的两阶段运输流程。
- 事件日志、热力图和飞行状态没有全量持久化/回放；运行参数大多不跨重启保存。
- 后续应重点完善 AI 分配约束、运输阶段执行、多机累计完成配额和严格的轨迹/碰撞校验。

