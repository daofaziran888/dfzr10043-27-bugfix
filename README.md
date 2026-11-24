# 织流工坊 · 网页流程自动化与RPA采集执行平台

面向RPA流程模板编排、元素定位漂移检测、机器人节点调度、执行步骤轨迹、凭据轮换、文件归集、采集批次确认与下游交接的流程自动化采集执行平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流程模板 | `flow_template` | 流程模板 | 模板编号、流程名称、起始地址 |
| 元素定位 | `element_locator` | 定位策略 | 定位编号、所属模板、元素名称 |
| 机器人节点 | `robot_node` | 执行节点 | 节点编号、节点名称、运行环境 |
| 流程执行 | `flow_run` | 执行实例 | 执行编号、关联模板、执行节点 |
| 步骤轨迹 | `step_trace` | 步骤轨迹 | 轨迹编号、关联执行、步骤序号 |
| 任务队列 | `task_queue` | 排队任务 | 队列编号、关联模板、优先级 |
| 凭据保管 | `credential_vault` | 登录凭据 | 凭据编号、适用站点、账号标识 |
| 表单字段 | `form_field` | 填充字段 | 字段编号、关联模板、表单名称 |
| 文件归集 | `file_drop` | 归集文件 | 归集编号、关联执行、文件名称 |
| 识别区域 | `ocr_region` | 识别区域 | 区域编号、关联模板、区域名称 |
| 排程计划 | `schedule_plan` | 排程计划 | 计划编号、关联模板、触发周期 |
| 异常规则 | `exception_rule` | 处置规则 | 规则编号、匹配特征、判定条件 |
| 重试策略 | `retry_policy` | 重试策略 | 策略编号、关联规则、最大次数 |
| 采集批次 | `capture_batch` | 采集批次 | 批次编号、关联执行、记录行数 |
| 字段映射 | `field_mapping` | 映射规则 | 映射编号、来源字段、目标字段 |
| 数据落库 | `data_landing` | 落库记录 | 落库编号、关联批次、目标存储 |
| 审计轨迹 | `audit_trail` | 审计条目 | 条目编号、操作对象、操作类型 |
| 资源额度 | `resource_pool` | 资源额度 | 额度编号、资源类型、总额度 |
| 版本发布 | `version_release` | 发布版本 | 发布编号、关联模板、版本号 |
| 运行指标 | `metric_board` | 指标读数 | 读数编号、统计维度、统计窗口 |
| 抽检样本 | `sample_check` | 抽检样本 | 样本编号、关联批次、抽样比例 |
| 交接包 | `handoff_pack` | 交接包 | 交接编号、关联批次、目标系统 |

## 文件明细与交接确认

采集批次确认和交接打包都以“真实通过完整性校验的文件”为准：

- `GET /api/capture_batch/{id}/manifest`：返回批次的有效文件明细
  （`files`）与被排除的坏文件（`excluded`），摘要按有效文件重算；
- `GET /api/handoff_pack/{id}/manifest`：回查交接包发出时的明细与摘要快照，
  迟到补件后不会被覆盖；
- `POST /api/capture_batch/{id}/actions {"action":"确认批次"}`：重新取数并按
  有效文件落确认摘要与明细，坏文件不进入确认集合；
- `POST /api/handoff_pack/{id}/actions {"action":"确认交接"}`：按清单引用的
  范围落回执，幂等；同一有效文件跨交接只交付一次，回执写回归集记录与批次。

坏文件保留在归集列表中供复核，但不会计入已接收统计。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
