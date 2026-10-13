import { useCallback, useEffect, useState } from 'react'
import { scenariosApi, verificationApi } from '../../api'
import { JournalEntry, VerificationReport, VerificationVerdict } from '../../types'
import './TruthJournalPanel.css'

const AUTO_REFRESH_MS = 5000

const VERDICT_LABEL: Record<VerificationVerdict, string> = {
  pass: 'сошлось',
  partial: 'частично',
  fail: 'расхождение',
}

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

function errorText(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: { message?: string; code?: string } | string } } })
    ?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  if (detail?.code) return detail.code
  return 'Сверка не удалась'
}

interface VerifyOutcome {
  verdict?: VerificationVerdict
  causes?: string
  state?: string
  outputs?: string
  error?: string
}

function summarize(r: VerificationReport): VerifyOutcome {
  const causes = Object.entries(r.cause_check.counts)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${k}`)
    .join(', ')
  const state = `${r.state_check.value_matched}/${r.state_check.present} значений`
  const outputs = r.outputs
    ? `выходы ${r.outputs.computed}/${r.outputs.total}`
    : 'выходы не считались'
  return { verdict: r.verdict, causes, state, outputs }
}

/** Журнал правды: причины, зафиксированные движком сценариев, и сверка со станцией. */
export default function TruthJournalPanel() {
  const [entries, setEntries] = useState<JournalEntry[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [verifying, setVerifying] = useState<Record<string, boolean>>({})
  const [outcomes, setOutcomes] = useState<Record<string, VerifyOutcome>>({})

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

  const verify = useCallback(async (key: string, entry: JournalEntry) => {
    setVerifying((prev) => ({ ...prev, [key]: true }))
    try {
      const report = await verificationApi.report(entry)
      setOutcomes((prev) => ({ ...prev, [key]: summarize(report) }))
    } catch (e) {
      setOutcomes((prev) => ({ ...prev, [key]: { error: errorText(e) } }))
    } finally {
      setVerifying((prev) => ({ ...prev, [key]: false }))
    }
  }, [])

  return (
    <div className="tj">
      <div className="tj-head">
        <h3>Журнал правды</h3>
        <span className="tj-sub">
          причины, зафиксированные при проигрывании сценариев; сверка снимает снапшот
          станции и сравнивает с ожидаемым состоянием
        </span>
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
              <th>Сверка</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => {
              const key = `${e.run_id}-${e.episode}`
              const outcome = outcomes[key]
              return (
                <tr key={key}>
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
                  <td className="tj-verify">
                    <button
                      type="button"
                      className="tj-verify-btn"
                      disabled={verifying[key] === true}
                      onClick={() => verify(key, e)}
                      title="Снять снапшот станции (live_batch), рассчитать и сверить с журналом"
                    >
                      {verifying[key] ? 'Сверка…' : 'Снапшот и сверка'}
                    </button>
                    {outcome && !outcome.error && outcome.verdict && (
                      <div className="tj-verify-result">
                        <span className={`tj-verdict tj-verdict-${outcome.verdict}`}>
                          {VERDICT_LABEL[outcome.verdict]}
                        </span>
                        <div className="tj-verify-detail">
                          причины: {outcome.causes || '—'}
                          <br />
                          состояние: {outcome.state}
                          <br />
                          {outcome.outputs}
                        </div>
                      </div>
                    )}
                    {outcome?.error && <div className="tj-verify-error">{outcome.error}</div>}
                  </td>
                </tr>
              )
            })}
            {entries.length === 0 && !err && (
              <tr>
                <td colSpan={5} className="tj-none">
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
