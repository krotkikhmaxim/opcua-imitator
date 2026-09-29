import { useCallback, useEffect, useState } from 'react'
import { scenariosApi } from '../api'
import { RecordedMode, ScenarioStatus } from '../types'
import './RecordedModesPanel.css'

const AUTO_REFRESH_MS = 5000

interface Props {
  onModeChanged: () => void
}

function formatDuration(ms: number): string {
  if (!ms) return '—'
  const totalSec = Math.round(ms / 1000)
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  return m > 0 ? `${m} мин ${s} с` : `${s} с`
}

function formatWhen(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export default function RecordedModesPanel({ onModeChanged }: Props) {
  const [modes, setModes] = useState<RecordedMode[]>([])
  const [status, setStatus] = useState<ScenarioStatus | null>(null)
  const [available, setAvailable] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const desc = await scenariosApi.describe()
      setModes(desc.modes)
      setStatus(desc.status)
      setAvailable(desc.available)
      setErr(desc.available ? null : (desc.unavailable_reason || 'сценарии недоступны'))
    } catch {
      /* backend недоступен — оставляем прошлое */
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, AUTO_REFRESH_MS)
    return () => clearInterval(interval)
  }, [refresh])

  const start = useCallback(
    async (mode: string) => {
      const title = modes.find((m) => m.mode === mode)?.title || mode
      setBusy(true)
      setErr(null)
      try {
        const s = await scenariosApi.startMode(mode)
        setStatus(s)
        onModeChanged()
      } catch (e: any) {
        setErr(e?.response?.data?.detail || `Ошибка запуска режима «${title}»`)
        refresh()
      } finally {
        setBusy(false)
      }
    },
    [modes, onModeChanged, refresh],
  )

  const stop = useCallback(async () => {
    setBusy(true)
    setErr(null)
    try {
      const s = await scenariosApi.stop()
      setStatus(s)
      onModeChanged()
    } catch (e: any) {
      setErr(e?.response?.data?.detail || 'Ошибка остановки')
      refresh()
    } finally {
      setBusy(false)
    }
  }, [onModeChanged, refresh])

  const running = status?.running ?? false
  const runningKind = running && status?.kind ? status.kind : null
  const matchingMode = runningKind ? modes.find((m) => m.mode === runningKind) : undefined
  const runningTitle = matchingMode?.title || runningKind

  return (
    <section className="rm">
      <div className="rm-head">
        <div>
          <h2>Режимы записей телеметрии</h2>
          <p className="rm-sub">Реальные записи режимов подъёмной машины — проигрываются в темпе записи</p>
        </div>
        <div className="rm-phase-wrap">
          <span className={`rm-phase rm-phase-${status?.phase || 'idle'}`}>
            {running ? `Идёт: ${runningTitle}` : status?.phase === 'finished' ? 'Завершено' : 'Простой'}
          </span>
          {running && (
            <button type="button" className="rm-stop-ghost" disabled={busy} onClick={stop}>
              Остановить
            </button>
          )}
        </div>
      </div>

      {err && <div className="rm-err">{err}</div>}

      <div className="rm-cards">
        {modes.map((m) => {
          const isRunning = runningKind === m.mode
          return (
            <div
              key={m.mode}
              className={`rm-card${isRunning ? ' rm-card-running' : ''}${m.fault ? ' rm-card-fault' : ''}`}
            >
              <div className="rm-card-head">
                <span className="rm-title" title={m.mode}>{m.title}</span>
                <span className={`rm-badge${m.fault ? ' rm-badge-fault' : ' rm-badge-ok'}`}>
                  {m.fault ? 'авария' : 'норма'}
                </span>
              </div>
              <div className="rm-meta">
                <span>{formatDuration(m.duration_ms)}</span>
                <span>{m.signals.length} сигн.</span>
                <span>{m.changes} смен</span>
              </div>
              <div className="rm-actions">
                <button
                  type="button"
                  className="rm-run"
                  disabled={!available || busy || running}
                  onClick={() => start(m.mode)}
                  title={m.expected_answer}
                >
                  {isRunning ? '…' : 'Запустить'}
                </button>
                {isRunning && (
                  <button type="button" className="rm-stop" disabled={busy} onClick={stop}>
                    Стоп
                  </button>
                )}
              </div>
              {isRunning && status && (
                <div className="rm-runinfo">
                  пуск {formatWhen(status.started_at)} · записано {status.writes}
                </div>
              )}
              <div className="rm-answer" title="Ожидаемый ответ (журнал правды)">
                {m.expected_answer}
              </div>
            </div>
          )
        })}
        {modes.length === 0 && <div className="rm-empty">Нет записей режимов</div>}
      </div>
    </section>
  )
}