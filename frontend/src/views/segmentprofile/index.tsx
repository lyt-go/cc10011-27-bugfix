import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/segmentprofile'
const columns = [
  "画像编号",
  "分群名称",
  "筛选规则",
  "画像维度",
  "对比基准",
  "更新周期",
  "覆盖对象数",
  "画像快照"
]
const statuses = ["草稿", "画像评审中", "已发布", "已下线"]
const snapshotStatuses = ["已同步", "需重算", "缺少快照"]

type Row = Record<string, unknown>

type OptionPayload = {
  画像维度: string[]
  更新周期: string[]
  对比基准候选: string[]
  筛选预设: { 预设编号: string; 预设名称: string; 状态: string; 版本号: number }[]
  人群数据: { 数据版本: string; 对象总数: number; 默认数据区间: string }
}

type Snapshot = {
  快照编号: string
  版本号: number
  状态: string
  规则文本: string
  规则指纹: string
  画像维度文本: string
  对比基准: string
  数据区间: string
  目标覆盖对象数: number
  基准覆盖对象数: number
  覆盖差异: number
  生成时间: string
  是否固化: boolean
  口径冲突: null | {
    历史版本: number
    历史覆盖对象数: number
    现行覆盖对象数: number
    变化项: string[]
    处理决定: string
    权衡依据: string
  }
  画像差异: {
    数据区间: string
    同区间校验: { 目标侧区间: string; 基准侧区间: string; 一致: boolean }
    目标覆盖对象数: number
    基准覆盖对象数: number
    覆盖差异: number
    重叠对象数: number
    目标独有对象数: number
    基准独有对象数: number
    Jaccard系数: number
    维度差异: { 维度: string; 取值差异: { 取值: string; 目标对象数: number; 基准对象数: number; 目标占比: number; 基准占比: number; 差异百分点: number }[] }[]
  }
  来源记录?: { 来源类型: string; 来源标识: string; 数据版本: string; 规则指纹: string; 数据区间: string; 记录数: number; 摘要: string }[]
}

const emptyForm = {
  画像编号: '',
  分群名称: '',
  筛选规则: '',
  画像维度: '',
  对比基准: '全量人群',
  更新周期: '按周',
  预设编号: '',
  数据区间: '',
}

export default function SegmentprofileView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [options, setOptions] = useState<OptionPayload | null>(null)
  const [form, setForm] = useState({ ...emptyForm })
  const [showForm, setShowForm] = useState(false)
  const [active, setActive] = useState<Row | null>(null)
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null)
  const [sources, setSources] = useState<NonNullable<Snapshot['来源记录']>>([])
  const [audit, setAudit] = useState<{ 一致: boolean; 问题数: number; 问题: { 画像编号: string; 问题: string; 说明: string }[] } | null>(null)

  const filterFields = useMemo(() => ['画像编号', '分群名称'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (filters['画像编号']) query.set('keyword', filters['画像编号'])
    if (filters['分群名称']) query.set('keyword', filters['分群名称'])
    if (filters['状态']) query.set('status', filters['状态'])
    if (filters['快照状态']) query.set('snapshot_status', filters['快照状态'])
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('画像定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分群画像列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const runAudit = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/audit`)
      if (!response.ok) throw new Error('口径核对失败')
      setAudit(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '口径核对失败')
    }
  }, [])

  useEffect(() => {
    request(`${ENDPOINT}/options`)
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error('可选项读取失败'))))
      .then((payload: OptionPayload) => setOptions(payload))
      .catch(() => setOptions(null))
  }, [])

  const stats = useMemo(() => {
    const published = rows.filter((row) => row.status === '已发布').length
    const reviewing = rows.filter((row) => row.status === '画像评审中').length
    const stale = rows.filter((row) => row['快照状态'] !== '已同步').length
    return [
      { label: '画像定义总数', value: total },
      { label: '已发布画像', value: published },
      { label: '评审中画像', value: reviewing },
      { label: '快照待重算', value: stale },
    ]
  }, [rows, total])

  const activeSnapshot = useMemo(
    () => snapshots.find((item) => item.版本号 === selectedVersion) ?? snapshots[0] ?? null,
    [snapshots, selectedVersion]
  )

  async function openDetail(row: Row) {
    setActive(row)
    setErrorMessage('')
    setNotice('')
    setSources([])
    try {
      const response = await request(`${ENDPOINT}/${row.id}/snapshots`)
      if (!response.ok) throw new Error('画像快照读取失败')
      const payload = await response.json()
      setSnapshots(payload.items ?? [])
      setSelectedVersion(payload.items?.[0]?.版本号 ?? null)
    } catch (error) {
      setSnapshots([])
      setErrorMessage(error instanceof Error ? error.message : '画像快照读取失败')
    }
  }

  useEffect(() => {
    if (!active || !activeSnapshot) {
      setSources([])
      return
    }
    request(`${ENDPOINT}/${active.id}/sources`)
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error('来源记录读取失败'))))
      .then((payload) => setSources(payload.items ?? []))
      .catch(() => setSources([]))
  }, [active, activeSnapshot?.快照编号])

  async function postAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '分群画像动作未生效，请稍后重试')
      }
      setNotice(payload.message)
      await reload()
      if (active && active.id === row.id) {
        await openDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分群画像操作失败')
    }
  }

  async function submitForm() {
    setErrorMessage('')
    setNotice('')
    const values: Record<string, string> = {}
    Object.entries(form).forEach(([key, value]) => {
      if (String(value).trim()) values[key] = value
    })
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '画像定义登记失败')
      }
      setNotice(payload.message)
      setForm({ ...emptyForm })
      setShowForm(false)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '画像定义登记失败')
    }
  }

  return (
    <section className="page" data-module="segmentprofile">
      <header className="page-head">
        <div>
          <h2>分群画像管理</h2>
          <p className="page-desc">
            画像定义保存规则、维度、对比基准与更新周期，覆盖对象数按同一数据区间重算并可追溯到快照版本。
          </p>
          {options && (
            <p className="page-desc">
              数据口径：人群 {options.人群数据.数据版本}（{options.人群数据.对象总数} 人），默认区间 {options.人群数据.默认数据区间}
            </p>
          )}
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowForm((prev) => !prev)}>
            {showForm ? '收起登记表单' : '登记画像定义'}
          </button>
          <button className="btn" type="button" onClick={() => void runAudit()}>
            口径核对
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分群画像清单
          </button>
        </div>
      </header>

      {showForm && (
        <form
          className="panel"
          onSubmit={(event) => {
            event.preventDefault()
            void submitForm()
          }}
        >
          <div className="form-grid">
            <label className="filter-item">
              <span>画像编号</span>
              <input value={form.画像编号} onChange={(event) => setForm({ ...form, 画像编号: event.target.value })} placeholder="SEGM-1001" />
            </label>
            <label className="filter-item">
              <span>分群名称</span>
              <input value={form.分群名称} onChange={(event) => setForm({ ...form, 分群名称: event.target.value })} placeholder="高活跃会员画像" />
            </label>
            <label className="filter-item wide">
              <span>筛选规则（字段=取值，多条用；分隔）</span>
              <input value={form.筛选规则} onChange={(event) => setForm({ ...form, 筛选规则: event.target.value })} placeholder="活跃度=高活跃；会员等级=金卡,银卡" />
            </label>
            <label className="filter-item">
              <span>画像维度</span>
              <input value={form.画像维度} onChange={(event) => setForm({ ...form, 画像维度: event.target.value })} placeholder="城市,会员等级" list="dimension-options" />
              <datalist id="dimension-options">
                {options?.画像维度.map((item) => <option key={item} value={item} />)}
              </datalist>
            </label>
            <label className="filter-item">
              <span>对比基准</span>
              <input value={form.对比基准} onChange={(event) => setForm({ ...form, 对比基准: event.target.value })} list="baseline-options" />
              <datalist id="baseline-options">
                {options?.对比基准候选.map((item) => <option key={item} value={item} />)}
              </datalist>
            </label>
            <label className="filter-item">
              <span>更新周期</span>
              <select value={form.更新周期} onChange={(event) => setForm({ ...form, 更新周期: event.target.value })}>
                {(options?.更新周期 ?? ['按日', '按周', '按月']).map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <label className="filter-item">
              <span>引用筛选预设（可选）</span>
              <select value={form.预设编号} onChange={(event) => setForm({ ...form, 预设编号: event.target.value })}>
                <option value="">不引用</option>
                {options?.筛选预设.map((item) => (
                  <option key={item.预设编号} value={item.预设编号}>
                    {item.预设编号} {item.预设名称}（v{item.版本号}/{item.状态}）
                  </option>
                ))}
              </select>
            </label>
            <label className="filter-item">
              <span>数据区间（可选，默认按周期推导）</span>
              <input value={form.数据区间} onChange={(event) => setForm({ ...form, 数据区间: event.target.value })} placeholder="2026-09-09~2026-09-15" />
            </label>
          </div>
          <div className="page-actions">
            <button className="btn primary" type="submit">保存并计算覆盖数</button>
            <button className="btn ghost" type="button" onClick={() => setForm({ ...emptyForm })}>清空</button>
          </div>
        </form>
      )}

      {audit && (
        <div className={`alert ${audit.一致 ? 'ok-alert' : ''}`}>
          <strong>口径核对：</strong>
          {audit.一致 ? '画像、快照、来源记录与事件分析消费方一致，没有覆盖数残留或规则丢失。' : `发现 ${audit.问题数} 个问题`}
          {audit.问题.length > 0 && (
            <ul className="issue-list">
              {audit.问题.map((item, index) => (
                <li key={`${item.画像编号}-${item.问题}-${index}`}>
                  {item.画像编号 || '—'}｜{item.问题}｜{item.说明}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>状态</span>
          <select value={filters['状态'] ?? ''} onChange={(event) => setFilters((prev) => ({ ...prev, 状态: event.target.value }))}>
            <option value="">全部</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>快照状态</span>
          <select value={filters['快照状态'] ?? ''} onChange={(event) => setFilters((prev) => ({ ...prev, 快照状态: event.target.value }))}>
            <option value="">全部</option>
            {snapshotStatuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>快照状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={active && active.id === row.id ? 'row-active' : undefined}>
              {columns.map((column) => <td key={column}>{formatCell(row[column])}</td>)}
              <td>
                <span className={`tag ${row['快照状态'] === '已同步' ? 'ok' : 'warn'}`}>{String(row['快照状态'] ?? '—')}</span>
              </td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>快照与差异</button>
                {(row['可执行动作'] as string[] | undefined)?.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void postAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无分群画像数据，可先登记画像定义</td>
            </tr>
          )}
        </tbody>
      </table>

      {active && (
        <section className="panel detail-panel">
          <header className="panel-head">
            <div>
              <h3>{String(active['画像编号'])} · {String(active['分群名称'])}</h3>
              <p className="page-desc">
                状态 {String(active.status)}｜维度 {String(active['画像维度'] ?? '—')}｜基准 {String(active['对比基准'] ?? '—')}｜
                周期 {String(active['更新周期'] ?? '—')}｜覆盖对象数 {String(active['覆盖对象数'] ?? '—')}
              </p>
            </div>
            <div className="page-actions">
              <button className="btn" type="button" onClick={() => void postAction('重算覆盖数', active)}>重算覆盖数</button>
              <button className="btn ghost" type="button" onClick={() => setActive(null)}>关闭</button>
            </div>
          </header>

          {snapshots.length === 0 && <p className="empty-state">还没有可追溯的画像快照</p>}

          {snapshots.length > 0 && (
            <>
              <div className="version-bar">
                {snapshots.map((item) => (
                  <button
                    key={item.快照编号}
                    type="button"
                    className={`btn ${item.版本号 === activeSnapshot?.版本号 ? 'primary' : 'ghost'}`}
                    onClick={() => setSelectedVersion(item.版本号)}
                  >
                    v{item.版本号} · {item.状态}
                  </button>
                ))}
              </div>

              {activeSnapshot && (
                <div className="snapshot-grid">
                  <article className="stat-card">
                    <span className="stat-label">目标覆盖对象数</span>
                    <strong className="stat-value">{activeSnapshot.目标覆盖对象数}</strong>
                    <span className="stat-label">{activeSnapshot.数据区间}</span>
                  </article>
                  <article className="stat-card">
                    <span className="stat-label">基准覆盖对象数</span>
                    <strong className="stat-value">{activeSnapshot.基准覆盖对象数}</strong>
                    <span className="stat-label">{activeSnapshot.对比基准}</span>
                  </article>
                  <article className="stat-card">
                    <span className="stat-label">覆盖差异</span>
                    <strong className="stat-value">{activeSnapshot.覆盖差异}</strong>
                    <span className="stat-label">Jaccard {activeSnapshot.画像差异.Jaccard系数}</span>
                  </article>
                  <article className="stat-card">
                    <span className="stat-label">同区间校验</span>
                    <strong className="stat-value">{activeSnapshot.画像差异.同区间校验.一致 ? '一致' : '不一致'}</strong>
                    <span className="stat-label">
                      {activeSnapshot.画像差异.同区间校验.目标侧区间} / {activeSnapshot.画像差异.同区间校验.基准侧区间}
                    </span>
                  </article>
                </div>
              )}

              {activeSnapshot?.口径冲突 && (
                <div className="alert">
                  <strong>口径冲突已处置：</strong>
                  {activeSnapshot.口径冲突.变化项.join('、')} 发生变化，历史覆盖数
                  {activeSnapshot.口径冲突.历史覆盖对象数}，现行覆盖数 {activeSnapshot.口径冲突.现行覆盖对象数}。
                  {activeSnapshot.口径冲突.处理决定}。
                  <div className="page-desc">{activeSnapshot.口径冲突.权衡依据}</div>
                </div>
              )}

              <div className="two-col">
                <div>
                  <h4>画像差异（同区间）</h4>
                  <table className="data-table">
                    <thead>
                      <tr><th>维度</th><th>取值</th><th>目标对象数</th><th>基准对象数</th><th>差异百分点</th></tr>
                    </thead>
                    <tbody>
                      {activeSnapshot?.画像差异.维度差异.flatMap((group) =>
                        group.取值差异.map((item) => (
                          <tr key={`${group.维度}-${item.取值}`}>
                            <td>{group.维度}</td>
                            <td>{item.取值}</td>
                            <td>{item.目标对象数}</td>
                            <td>{item.基准对象数}</td>
                            <td>{item.差异百分点}</td>
                          </tr>
                        ))
                      )}
                      {!activeSnapshot?.画像差异.维度差异.length && (
                        <tr><td className="empty-state" colSpan={5}>该版本没有维度差异数据</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
                <div>
                  <h4>来源记录</h4>
                  <table className="data-table">
                    <thead>
                      <tr><th>来源类型</th><th>标识</th><th>规则指纹</th><th>数据区间</th><th>记录数</th></tr>
                    </thead>
                    <tbody>
                      {sources.map((item, index) => (
                        <tr key={`${item.来源类型}-${index}`}>
                          <td>{item.来源类型}</td>
                          <td>{item.来源标识}</td>
                          <td>{item.规则指纹}</td>
                          <td>{item.数据区间}</td>
                          <td>{item.记录数}</td>
                        </tr>
                      ))}
                      {sources.length === 0 && (
                        <tr><td className="empty-state" colSpan={5}>暂无来源记录</td></tr>
                      )}
                    </tbody>
                  </table>
                  <h4>版本明细</h4>
                  <table className="data-table">
                    <tbody>
                      <tr><th>快照编号</th><td>{activeSnapshot?.快照编号}</td></tr>
                      <tr><th>规则文本</th><td>{activeSnapshot?.规则文本}</td></tr>
                      <tr><th>规则指纹</th><td>{activeSnapshot?.规则指纹}</td></tr>
                      <tr><th>是否固化</th><td>{activeSnapshot?.是否固化 ? '已随发布固化' : '未固化'}</td></tr>
                      <tr><th>生成时间</th><td>{activeSnapshot?.生成时间}</td></tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条分群画像记录</span>
        <span>
          {notice && <span className="ok-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.join('、')
  return String(value)
}
