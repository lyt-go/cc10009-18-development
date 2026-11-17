import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const JOB_ENDPOINT = '/api/rendertask'
const RESULT_ENDPOINT = '/api/renderresult'
const QUEUE_ENDPOINT = '/api/renderqueue'
const LANDING_ENDPOINT = '/api/renderlanding'

const jobColumns = [
  '作业编号',
  '目标URL',
  '浏览器版本',
  '视口宽度',
  '视口高度',
  '等待策略',
  '脚本注入',
  '超时阈值秒',
]
const jobActions = [
  '提交排队',
  '启动渲染',
  '完成渲染',
  '终止渲染',
  '标记超时',
  '标记连接中断',
  '标记页面异常',
]
const jobStatuses = ['草稿', '排队中', '渲染中', '已完成', '已超时']

const resultColumns = ['结果编号', '作业编号', '目标URL', '等待策略', '页面版本', '结果版本', 'status', '结果边界', '排队边界', '诊断数', '处理次数']
const queueColumns = ['队列编号', '作业编号', '队列位置', '节点标识', '槽位标识', '等待策略', '等待策略版本', 'status', '容量已占位数', '节点容量', '容量占用', '容量回收原因', '更新时间']
const landingColumns = ['落点编号', '作业编号', '队列编号', '结果编号', '结果版本', 'status', '容量状态', '已占槽位数', '节点容量', '快照状态', '处理状态', '处理次数', '最后同步时间']
const resultStatuses = ['待渲染', '排队中', '渲染中', '已完成', '已超时', '已终止', '已失败']
const queueStatuses = ['排队中', '渲染中', '已完成', '已超时', '已终止', '已失败']
const landingStatuses = ['已占用', '已释放', '已同步']

type Row = Record<string, unknown>
type TabKey = 'jobs' | 'results' | 'queues' | 'landing'

type Column = { key: string; label: string; render?: (row: Row) => string }

const numberText = (value: unknown) => (value === null || value === undefined || value === '' ? '—' : String(value))

const jobColumnsDef: Column[] = jobColumns.map((key) => ({ key, label: key }))
const resultColumnsDef: Column[] = [
  { key: '结果编号', label: '结果编号' },
  { key: '作业编号', label: '作业编号' },
  { key: '目标URL', label: '目标URL' },
  { key: '等待策略', label: '等待策略' },
  { key: '等待策略版本', label: '策略版本' },
  { key: '页面版本', label: '页面版本' },
  { key: '结果版本', label: '结果版本' },
  { key: 'status', label: '结果状态' },
  { key: '结果边界', label: '结果边界' },
  { key: '排队边界', label: '排队边界' },
  { key: '诊断信息', label: '诊断', render: (row) => `${(row['诊断信息'] as unknown[] | undefined)?.length ?? 0} 条` },
  { key: '处理次数', label: '处理次数' },
]
const queueColumnsDef: Column[] = queueColumns.map((key) => ({
  key,
  label: key === 'status' ? '队列状态' : key,
  render:
    key === '容量占用' || key === '容量已占位数' || key === '节点容量' || key === '队列位置'
      ? (row) => numberText(row[key])
      : undefined,
}))
const landingColumnsDef: Column[] = landingColumns.map((key) => ({
  key,
  label: key === 'status' ? '落点状态' : key,
}))

const filtersByTab: Record<TabKey, string[]> = {
  jobs: ['作业编号', '等待策略'],
  results: ['结果编号', '作业编号'],
  queues: ['队列编号', '作业编号'],
  landing: ['落点编号', '作业编号'],
}

function matches(row: Row, filters: Record<string, string>): boolean {
  return Object.entries(filters).every(([field, value]) => {
    if (!value.trim()) return true
    return String(row[field] ?? '').includes(value.trim())
  })
}

export default function RendertaskView() {
  const [tab, setTab] = useState<TabKey>('jobs')
  const [jobs, setJobs] = useState<Row[]>([])
  const [results, setResults] = useState<Row[]>([])
  const [queues, setQueues] = useState<Row[]>([])
  const [landing, setLanding] = useState<Row[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selectedResult, setSelectedResult] = useState<Row | null>(null)

  const activeFilters = useMemo(() => filtersByTab[tab], [tab])
  const filterFields = useMemo(
    () => (tab === 'jobs' ? jobColumns.slice(0, 3) : activeFilters),
    [tab, activeFilters],
  )

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      // 四个数据集一起读：概览卡片和页签切换都无需再等请求，状态口径也保持一致。
      const [jobsPayload, resultsPayload, queuesPayload, landingPayload] = await Promise.all([
        request(`${JOB_ENDPOINT}?size=200`).then((response) => {
          if (!response.ok) throw new Error('渲染作业读取失败')
          return response.json()
        }),
        request(`${RESULT_ENDPOINT}?size=200`).then((response) => {
          if (!response.ok) throw new Error('渲染结果读取失败')
          return response.json()
        }),
        request(`${QUEUE_ENDPOINT}?size=200`).then((response) => {
          if (!response.ok) throw new Error('队列状态读取失败')
          return response.json()
        }),
        request(`${LANDING_ENDPOINT}?size=200`).then((response) => {
          if (!response.ok) throw new Error('调度落点读取失败')
          return response.json()
        }),
      ])
      setJobs(jobsPayload.items ?? [])
      setResults(resultsPayload.items ?? [])
      setQueues(queuesPayload.items ?? [])
      setLanding(landingPayload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '渲染任务列表读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const visibleRows = useMemo(() => {
    const source = tab === 'jobs' ? jobs : tab === 'results' ? results : tab === 'queues' ? queues : landing
    return source.filter((row) => matches(row, filters))
  }, [tab, jobs, results, queues, landing, filters])

  const visibleTotal = visibleRows.length

  async function post(path: string, body?: unknown): Promise<Row | null> {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(path, {
        method: 'POST',
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload?.detail ? String(payload.detail) : '渲染任务动作未生效，请稍后重试')
      }
      if (payload?.ok === false) {
        setErrorMessage(String(payload.message))
        await reload()
        return null
      }
      setNotice(String(payload.message ?? '操作已生效'))
      await reload()
      return payload as Row
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '渲染任务操作失败')
      return null
    }
  }

  async function runJobAction(action: string, row: Row) {
    await post(`${JOB_ENDPOINT}/${row.id}/actions`, { values: { action } })
  }

  async function runResultAction(kind: 'validate-wait' | 'finalize' | 'process' | 'dispatch', row: Row) {
    const payload = await post(`${RESULT_ENDPOINT}/${row.id}/${kind}`)
    if (kind === 'validate-wait' && payload?.entry) {
      setSelectedResult(payload.entry as Row)
    }
    if (kind === 'finalize' && payload?.entry) {
      setSelectedResult(payload.entry as Row)
    }
  }

  async function refreshQueue(row: Row) {
    await post(`${QUEUE_ENDPOINT}/${row.id}/refresh`)
  }

  const columns = tab === 'jobs' ? jobColumnsDef : tab === 'results' ? resultColumnsDef : tab === 'queues' ? queueColumnsDef : landingColumnsDef
  const statuses = tab === 'jobs' ? jobStatuses : tab === 'results' ? resultStatuses : tab === 'queues' ? queueStatuses : landingStatuses

  const summary = useMemo(() => {
    const resultRows = results
    return [
      { label: '渲染结果数', value: resultRows.length },
      { label: '成功结果', value: resultRows.filter((row) => row['成功页面']).length },
      { label: '队列占用中', value: queues.filter((row) => row['容量占用']).length },
      { label: '可重试诊断', value: resultRows.filter((row) => ((row['诊断信息'] as unknown[] | undefined)?.length ?? 0) > 0).length },
    ]
  }, [results, queues])

  return (
    <section className="page" data-module="rendertask">
      <header className="page-head">
        <div>
          <h2>渲染任务管理</h2>
          <p className="page-desc">
            管理需要浏览器渲染的页面抓取作业：等待条件核验、超时处置、队列容量与调度落点快照。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${JOB_ENDPOINT}/export`, '_blank')}>
            导出渲染任务清单
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${RESULT_ENDPOINT}/export`, '_blank')}>
            导出渲染结果
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${QUEUE_ENDPOINT}/export`, '_blank')}>
            导出队列状态
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${LANDING_ENDPOINT}/export`, '_blank')}>
            导出调度落点清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {summary.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <div className="tab-bar">
        {([
          ['jobs', '渲染作业'],
          ['results', '渲染结果'],
          ['queues', '队列状态'],
          ['landing', '调度落点'],
        ] as [TabKey, string][]).map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={`tab${tab === key ? ' active' : ''}`}
            onClick={() => { setTab(key); setFilters({}); setSelectedResult(null) }}
          >
            {label}
          </button>
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
        {tab !== 'jobs' && (
          <label className="filter-item">
            <span>状态</span>
            <select
              value={filters['status'] ?? ''}
              onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
            >
              <option value="">全部状态</option>
              {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
            </select>
          </label>
        )}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column.key}>{column.label}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column.key}>
                  {column.render ? column.render(row) : numberText(row[column.key])}
                </td>
              ))}
              <td className="row-actions">
                {tab === 'jobs' && jobActions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runJobAction(action, row)}>
                    {action}
                  </button>
                ))}
                {tab === 'results' && (
                  <>
                    <button className="link" type="button" onClick={() => void runResultAction('validate-wait', row)}>
                      校验等待条件
                    </button>
                    <button className="link" type="button" onClick={() => void runResultAction('finalize', row)}>
                      生成结果
                    </button>
                    <button className="link" type="button" onClick={() => void runResultAction('process', row)}>
                      处理结果
                    </button>
                    <button className="link" type="button" onClick={() => void runResultAction('dispatch', row)}>
                      同步落点
                    </button>
                    <button className="link" type="button" onClick={() => setSelectedResult(row)}>
                      查看诊断
                    </button>
                  </>
                )}
                {tab === 'queues' && (
                  <button className="link" type="button" onClick={() => void refreshQueue(row)}>
                    刷新容量
                  </button>
                )}
                {tab === 'landing' && (
                  <button className="link" type="button" onClick={() => setSelectedResult(row['快照'] as Row ?? null)}>
                    查看快照
                  </button>
                )}
              </td>
            </tr>
          ))}
          {visibleRows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>当前条件下没有数据</td>
            </tr>
          )}
        </tbody>
      </table>

      {selectedResult && (
        <section className="detail-panel">
          <header className="detail-head">
            <h3>
              结果明细
              {' '}
              {String(selectedResult['结果编号'] ?? selectedResult['快照引用'] ?? '')}
            </h3>
            <button className="btn ghost" type="button" onClick={() => setSelectedResult(null)}>关闭</button>
          </header>
          <div className="detail-grid">
            <div>
              <h4>等待条件</h4>
              {Array.isArray(selectedResult['等待条件']) ? (
                <ul className="detail-list">
                  {(selectedResult['等待条件'] as Row[]).map((item, index) => (
                    <li key={index}>
                      {item['满足'] ? '✅' : '⏳'} {String(item['条件'] ?? '')}
                      <span className="muted"> — {String(item['证据'] ?? '')}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">无等待条件记录</p>
              )}
              <p className="muted">
                已满足：{((selectedResult['已满足等待条件'] as string[] | undefined) ?? []).join('、') || '—'}
                <br />
                未满足：{((selectedResult['未满足等待条件'] as string[] | undefined) ?? []).join('、') || '—'}
              </p>
            </div>
            <div>
              <h4>版本线</h4>
              {Array.isArray(selectedResult['版本历史']) ? (
                <ul className="detail-list">
                  {(selectedResult['版本历史'] as Row[]).map((item, index) => (
                    <li key={index}>
                      v{String(item['结果版本'] ?? '')} · {String(item['状态'] ?? '')} · {String(item['结果边界'] ?? '')}
                      <span className="muted">（{String(item['生成时间'] ?? '')}）</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">无版本历史</p>
              )}
            </div>
            <div>
              <h4>诊断与重试边界</h4>
              {Array.isArray(selectedResult['诊断信息']) && (selectedResult['诊断信息'] as Row[]).length > 0 ? (
                <ul className="detail-list">
                  {(selectedResult['诊断信息'] as Row[]).map((item, index) => (
                    <li key={index}>
                      【{String(item['类型'] ?? '')}】{String(item['详情'] ?? '')}
                      <br />
                      <span className="muted">
                        可重试：{item['可重试'] ? '是' : '否'} · 重试令牌：{String(item['重试令牌'] ?? '—')}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">无诊断信息</p>
              )}
              {Boolean(selectedResult['重试边界']) && (
                <pre className="detail-json">
                  {JSON.stringify(selectedResult['重试边界'], null, 2)}
                </pre>
              )}
            </div>
          </div>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {visibleTotal} 条渲染记录</span>
        <span>
          {notice && <span className="notice-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
