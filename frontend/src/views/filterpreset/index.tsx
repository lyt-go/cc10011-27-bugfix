import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/filterpreset'
const columns = [
  "预设编号",
  "预设名称",
  "维度条件组",
  "时间范围",
  "排序规则",
  "显示列",
  "共享状态",
  "使用次数"
]
const actions = ["提交预设评审", "发布预设", "废弃预设"]
const statuses = ["草稿", "预设评审中", "已发布", "已废弃"]

type Row = Record<string, unknown>

type Preview = {
  预设编号: string
  版本号: number
  规则文本: string
  规则指纹: string
  状态: string
  数据区间: string
  覆盖对象数: number
  维度分布: { 维度: string; 取值分布: { 取值: string; 对象数: number; 占比: number }[] }[]
  人群数据版本: string
}

type Referencing = {
  画像编号: string
  分群名称: string
  状态: string
  画像快照: string | null
  覆盖对象数: number | null
  快照版本: number | null
}

export default function FilterpresetView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [previews, setPreviews] = useState<Record<string, Preview>>({})
  const [active, setActive] = useState<{ preset: Row; referencing: Referencing[]; preview: Preview | null } | null>(null)
  const filterFields = useMemo(() => ['预设编号', '预设名称'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (filters['预设编号']) query.set('keyword', filters['预设编号'])
    if (filters['预设名称']) query.set('keyword', filters['预设名称'])
    if (filters['状态']) query.set('status', filters['状态'])
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('预设模板列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '筛选预设列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => {
    const published = rows.filter((row) => row.status === '已发布').length
    const reviewing = rows.filter((row) => row.status === '预设评审中').length
    const referenced = rows.filter((row) => (row['引用画像'] as Referencing[] | undefined)?.length).length
    return [
      { label: '预设模板总数', value: total },
      { label: '已固化预设', value: published },
      { label: '评审中预设', value: reviewing },
      { label: '被画像引用', value: referenced },
    ]
  }, [rows, total])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '筛选预设动作未生效，请稍后重试')
      }
      setNotice(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '筛选预设操作失败')
    }
  }

  async function openPreview(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/preview`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail ?? '预设预览读取失败')
      setPreviews((prev) => ({ ...prev, [String(row.id)]: payload }))
      const detail = await request(`${ENDPOINT}/${row.id}`)
      const detailPayload = detail.ok ? await detail.json() : {}
      setActive({
        preset: row,
        referencing: (detailPayload['引用画像'] as Referencing[] | undefined) ?? (row['引用画像'] as Referencing[]) ?? [],
        preview: payload,
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '预设预览读取失败')
    }
  }

  return (
    <section className="page" data-module="filterpreset">
      <header className="page-head">
        <div>
          <h2>筛选预设管理</h2>
          <p className="page-desc">
            预设口径与分群画像共用同一取数链路；发布后规则固化，预览的覆盖对象数与引用它的画像快照一致。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('预设模板登记入口尚未接入审批流，可先通过接口或画像登记引用')}>
            登记预设模板
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出筛选预设清单
          </button>
        </div>
      </header>

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
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>版本</th>
            <th>规则指纹</th>
            <th>引用画像</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const referencing = (row['引用画像'] as Referencing[] | undefined) ?? []
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{formatCell(row[column])}</td>)}
                <td>v{String(row['版本号'] ?? 1)}</td>
                <td>{String(row['规则指纹'] ?? '—')}</td>
                <td>
                  {referencing.length
                    ? referencing.map((item) => `${item.画像编号}(${item.状态})`).join('、')
                    : '—'}
                </td>
                <td className="row-actions">
                  <button className="link" type="button" onClick={() => void openPreview(row)}>口径预览</button>
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无筛选预设数据，可先登记预设模板</td>
            </tr>
          )}
        </tbody>
      </table>

      {active && active.preview && (
        <section className="panel detail-panel">
          <header className="panel-head">
            <div>
              <h3>{active.preview.预设编号} · {String(active.preset['预设名称'])}</h3>
              <p className="page-desc">
                规则 {active.preview.规则文本}｜指纹 {active.preview.规则指纹}｜区间 {active.preview.数据区间}｜
                覆盖对象数 {active.preview.覆盖对象数}｜人群 {active.preview.人群数据版本}
              </p>
            </div>
            <button className="btn ghost" type="button" onClick={() => setActive(null)}>关闭</button>
          </header>
          <div className="two-col">
            <div>
              <h4>按预设口径的维度分布</h4>
              <table className="data-table">
                <thead><tr><th>维度</th><th>取值</th><th>对象数</th><th>占比</th></tr></thead>
                <tbody>
                  {active.preview.维度分布.flatMap((group) =>
                    group.取值分布.map((item) => (
                      <tr key={`${group.维度}-${item.取值}`}>
                        <td>{group.维度}</td>
                        <td>{item.取值}</td>
                        <td>{item.对象数}</td>
                        <td>{item.占比}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
            <div>
              <h4>引用该预设的画像</h4>
              <table className="data-table">
                <thead><tr><th>画像编号</th><th>状态</th><th>快照版本</th><th>覆盖对象数</th></tr></thead>
                <tbody>
                  {active.referencing.map((item) => (
                    <tr key={item.画像编号}>
                      <td>{item.画像编号}</td>
                      <td>{item.状态}</td>
                      <td>{item.快照版本 ? `v${item.快照版本}` : '—'}</td>
                      <td>{item.覆盖对象数 ?? '—'}</td>
                    </tr>
                  ))}
                  {active.referencing.length === 0 && (
                    <tr><td className="empty-state" colSpan={4}>暂无画像引用该预设</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条筛选预设记录</span>
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
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
