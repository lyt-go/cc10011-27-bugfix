import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/eventanalysis'
const columns = [
  "模型编号",
  "事件名称",
  "分组维度",
  "度量指标",
  "时间粒度",
  "归因窗口",
  "置信水平",
  "最小样本量"
]
const actions = ["提交模型评审", "启用模型", "归档模型"]
const statuses = ["草稿", "模型评审中", "已启用", "已归档"]

type Row = Record<string, unknown>

type Comparison = {
  模型编号: string
  事件名称: string
  画像编号: string
  快照编号: string
  快照版本: number
  数据区间: string
  覆盖对象数: number
  基准覆盖对象数: number
  画像差异: {
    数据区间: string
    同区间校验: { 目标侧区间: string; 基准侧区间: string; 一致: boolean }
    覆盖差异: number
    Jaccard系数: number
    维度差异: { 维度: string; 取值差异: { 取值: string; 目标对象数: number; 基准对象数: number; 差异百分点: number }[] }[]
  }
  来源记录: { 来源类型: string; 来源标识: string; 规则指纹: string; 数据区间: string; 记录数: number }[]
  口径一致性: Record<string, unknown>
}

export default function EventanalysisView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [comparison, setComparison] = useState<Comparison | null>(null)
  const [active, setActive] = useState<Row | null>(null)
  const [audit, setAudit] = useState<{
    一致: boolean
    问题数: number
    问题: { 画像编号: string; 问题: string; 说明: string }[]
    消费方: { 模型编号: string; 画像编号: string; 画像状态: string; 消费快照: string; 最新快照: string | null; 已同步: boolean }[]
  } | null>(null)
  const filterFields = useMemo(() => ['模型编号', '事件名称'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (filters['模型编号']) query.set('keyword', filters['模型编号'])
    if (filters['事件名称']) query.set('keyword', filters['事件名称'])
    if (filters['状态']) query.set('status', filters['状态'])
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('分析模型列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '事件分析列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAudit() {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/audit`)
      if (!response.ok) throw new Error('消费方核对失败')
      setAudit(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '消费方核对失败')
    }
  }

  const stats = useMemo(() => {
    const enabled = rows.filter((row) => row.status === '已启用').length
    const reviewing = rows.filter((row) => row.status === '模型评审中').length
    const stale = rows.filter((row) => row['快照同步状态'] === '待同步').length
    return [
      { label: '分析模型总数', value: total },
      { label: '已启用模型', value: enabled },
      { label: '评审中模型', value: reviewing },
      { label: '快照待同步', value: stale },
    ]
  }, [rows, total])

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
        throw new Error(payload.message ?? payload.detail ?? '事件分析动作未生效，请稍后重试')
      }
      setNotice(payload.message)
      await reload()
      if (active && active.id === row.id) {
        await openComparison(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '事件分析操作失败')
    }
  }

  async function openComparison(row: Row) {
    setErrorMessage('')
    setNotice('')
    setActive(row)
    try {
      const response = await request(`${ENDPOINT}/${row.id}/comparison`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail ?? '对比结果读取失败')
      setComparison(payload)
    } catch (error) {
      setComparison(null)
      setErrorMessage(error instanceof Error ? error.message : '对比结果读取失败')
    }
  }

  return (
    <section className="page" data-module="eventanalysis">
      <header className="page-head">
        <div>
          <h2>事件分析管理</h2>
          <p className="page-desc">
            事件分析只消费分群画像快照：覆盖对象数与画像差异来自同一版本、同一数据区间，已下线画像不会进入对比。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('分析模型登记入口尚未接入审批流，可先通过接口登记并关联已发布画像')}>
            登记分析模型
          </button>
          <button className="btn" type="button" onClick={() => void runAudit()}>
            核对画像快照
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出事件分析清单
          </button>
        </div>
      </header>

      {audit && (
        <div className={`alert ${audit.一致 ? 'ok-alert' : ''}`}>
          <strong>画像快照核对：</strong>
          {audit.一致 ? '所有事件分析模型都与画像最新快照一致。' : `发现 ${audit.问题数} 个问题`}
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
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>关联画像</th>
            <th>画像状态</th>
            <th>消费快照</th>
            <th>快照同步</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{formatCell(row[column])}</td>)}
              <td>{formatCell(row['画像编号'])}</td>
              <td>{formatCell(row['画像状态'])}</td>
              <td>
                {row['快照编号']
                  ? `${String(row['快照编号'])}（v${String(row['快照版本'] ?? '—')}）`
                  : '—'}
              </td>
              <td>
                <span className={`tag ${row['快照同步状态'] === '已同步' ? 'ok' : 'warn'}`}>
                  {String(row['快照同步状态'] ?? '—')}
                </span>
              </td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openComparison(row)}>对比结果</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void postAction(action, row)}>
                    {action}
                  </button>
                ))}
                {row['快照同步状态'] === '待同步' && (
                  <button className="link" type="button" onClick={() => void postAction('同步画像快照', row)}>
                    同步画像快照
                  </button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 5}>暂无事件分析数据，可先登记分析模型</td>
            </tr>
          )}
        </tbody>
      </table>

      {active && comparison && (
        <section className="panel detail-panel">
          <header className="panel-head">
            <div>
              <h3>{comparison.模型编号} · {comparison.事件名称}</h3>
              <p className="page-desc">
                画像 {comparison.画像编号}｜快照 {comparison.快照编号}（v{comparison.快照版本}）｜区间 {comparison.数据区间}｜
                覆盖对象数 {comparison.覆盖对象数}｜基准 {comparison.基准覆盖对象数}
              </p>
            </div>
            <button className="btn ghost" type="button" onClick={() => { setActive(null); setComparison(null) }}>关闭</button>
          </header>
          <div className="alert">
            同区间校验：{comparison.画像差异.同区间校验.一致 ? '一致' : '不一致'}
            （{comparison.画像差异.同区间校验.目标侧区间} / {comparison.画像差异.同区间校验.基准侧区间}），
            覆盖差异 {comparison.画像差异.覆盖差异}，Jaccard {comparison.画像差异.Jaccard系数}
          </div>
          <div className="two-col">
            <div>
              <h4>画像差异</h4>
              <table className="data-table">
                <thead><tr><th>维度</th><th>取值</th><th>目标对象数</th><th>基准对象数</th><th>差异百分点</th></tr></thead>
                <tbody>
                  {comparison.画像差异.维度差异.flatMap((group) =>
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
                  {comparison.画像差异.维度差异.length === 0 && (
                    <tr><td className="empty-state" colSpan={5}>该快照没有维度差异数据</td></tr>
                  )}
                </tbody>
              </table>
            </div>
            <div>
              <h4>来源记录</h4>
              <table className="data-table">
                <thead><tr><th>来源类型</th><th>标识</th><th>规则指纹</th><th>数据区间</th><th>记录数</th></tr></thead>
                <tbody>
                  {comparison.来源记录.map((item, index) => (
                    <tr key={`${item.来源类型}-${index}`}>
                      <td>{item.来源类型}</td>
                      <td>{item.来源标识}</td>
                      <td>{item.规则指纹}</td>
                      <td>{item.数据区间}</td>
                      <td>{item.记录数}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h4>口径一致性</h4>
              <table className="data-table">
                <tbody>
                  {Object.entries(comparison.口径一致性).map(([key, value]) => (
                    <tr key={key}><th>{key}</th><td>{String(value)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条事件分析记录</span>
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
