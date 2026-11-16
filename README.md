# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
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
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则、画像维度、对比基准、更新周期、覆盖对象数 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 分群画像共享链路

分群画像、筛选预设、事件分析共用 `backend/app/services/profilechain.py` 这一条取数链路，
人群样例数据与规则解析在 `backend/app/population.py`：

- **定义保存**：新建画像会完整保存筛选规则、画像维度、对比基准、更新周期与数据区间，
  并立即生成首个快照；覆盖对象数只按快照计算，接口提交的覆盖数不会直接采用。
- **同一区间**：目标分群与对比基准都在同一数据区间取数，`画像差异.同区间校验` 会明确
  给出两侧区间；事件分析消费快照时也会校验请求区间与快照区间一致。
- **冲突权衡**：历史覆盖数与现行规则/区间对不上时，旧版本保留为历史快照，
  新快照记录 `口径冲突`（历史覆盖数、现行覆盖数、变化项、处理决定与权衡依据），
  新对比只使用同区间重算值。
- **版本与事务**：同一分群、同一口径、同一区间只保留一个有效版本；`幂等键` 用于增量任务
  重试去重。快照、来源记录与画像行在同一个 `store.transaction()` 里写入，
  失败整体回滚，不会留下残缺版本。
- **状态流转**：草稿 → 画像评审中 → 已发布 → 已下线，发布后不可回退，
  已发布画像的定义与取数区间随发布快照固化。
- **消费方同步**：事件分析只能关联已发布画像；画像发布新版本后消费方显示「待同步」，
  可用 `POST /api/eventanalysis/{id}/actions {"action": "同步画像快照"}` 刷新。

相关接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/segmentprofile/options` | 维度、周期、基准候选、预设与人群数据口径 |
| `PUT /api/segmentprofile/{id}` | 修改定义并按新口径重算覆盖数 |
| `GET /api/segmentprofile/{id}/snapshots` | 快照版本列表（含口径冲突） |
| `GET /api/segmentprofile/{id}/difference` | 同区间画像差异与来源记录 |
| `GET /api/segmentprofile/{id}/sources` | 快照引用的目标人群、规则、基准、预设版本 |
| `GET /api/segmentprofile/audit` | 画像列表与快照/消费方口径核对 |
| `GET /api/filterpreset/{id}/preview` | 预设口径预览（与画像同一链路） |
| `GET /api/eventanalysis/{id}/comparison` | 消费画像快照的对比结果 |
| `GET /api/eventanalysis/audit` | 事件分析消费方快照核对 |

回归测试：

```bash
cd backend
.venv/bin/python -m unittest tests.test_profilechain -v
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
