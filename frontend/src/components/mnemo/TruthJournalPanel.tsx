import { useCallback, useEffect, useState } from 'react'
import { scenariosApi } from '../../api'
import { JournalEntry } from '../../types'
import './TruthJournalPanel.css'

const AUTO_REFRESH_MS = 5000

function formatWhen(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function formatCause(c: { before: unknown; after: unknown }): string {
  const f = (v: unknown) => (typeof v === 'boolean' ? (v ? '1' : '0') : String(v))
  return `${f(c.before)} → ${f(c.after)}`
}

/** Журнал правды: причины, зафиксированные движком сценариев. */
export default function TruthJournalPanel() {
  const [entries, setEntries] = useState<JournalEntry[]>([])
  const [err, setErr] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setEntries(await scenariosApi.journal(50))
      setErr(null)
    } catch {
      setErr('Журнал недоступен')
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, AUTO_REFRESH_MS)
    return () => clearInterval(interval)
  }, [refresh])

  return (
    <div className="tj">
      <div className="tj-head">
        <h3>Журнал правды</h3>
        <span className="tj-sub">причины, зафиксированные при проигрывании сценариев</span>
        <button type="button" className="tj-refresh" onClick={refresh}>
          Обновить
        </button>
      </div>
      {err && <div className="tj-err">{err}</div>}
      <div className="tj-table-wrap">
        <table className="tj-table">
          <thead>
            <tr>
              <th>Время причины</th>
              <th>Сценарий</th>
              <th>Причины</th>
              <th>Остановка</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={`${e.run_id}-${e.episode}`}>
                <td className="tj-mono">{formatWhen(e.cause_at)}</td>
                <td>
                  <span className={`tj-kind${e.fault ? ' tj-kind-fault' : ''}`}>{e.title}</span>
                  <div className="tj-meta">
                    эп. {e.episode} · seed {e.seed}
                  </div>
                </td>
                <td>
                  {e.causes.map((c) => (
                    <div key={c.signal_id} className="tj-cause" title={`${c.signal_id} (${c.project_tag ?? ''})`}>
                      <span className="tj-cause-name">{c.name || c.signal_id}</span>
                      <span className="tj-mono">{formatCause(c)}</span>
                    </div>
                  ))}
                  {e.causes.length === 0 && <span className="tj-none">—</span>}
                </td>
                <td className="tj-mono">{formatWhen(e.stopped_at)}</td>
              </tr>
            ))}
            {entries.length === 0 && !err && (
              <tr>
                <td colSpan={4} className="tj-none">
                  Пока пусто — запустите сценарий с пульта
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
