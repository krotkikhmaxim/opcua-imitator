import { useCallback, useEffect, useState } from 'react'
import { scenariosApi } from '../api'
import { RecordedMode, ScenarioStatus } from '../types'
import './RecordedModesPanel.css'

const AUTO_REFRESH_MS = 5000

interface Props {
  onModeChanged: () => void
}

interface Kind {
  kind: string
  title: string
  fault: boolean
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
  const [kinds, setKinds] = useState<Kind[]>([])
  const [status, setStatus] = useState<ScenarioStatus | null>(null)
  const [available, setAvailable] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const desc = await scenariosApi.describe()
      setModes(desc.modes)
      setKinds(desc.kinds)
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

  const runAndSync = useCallback(
    async (run: () => Promise<ScenarioStatus>, onFail: string) => {
      setBusy(true)
      setErr(null)
      try {
        setStatus(await run())
        onModeChanged()
      } catch (e: any) {
        setErr(e?.response?.data?.detail || onFail)
        refresh()
      } finally {
        setBusy(false)
      }
    },
    [onModeChanged, refresh],
  )

  const startMode = useCallback(
    (id: string) =>
      runAndSync(() => scenariosApi.startMode(id), `Ошибка запуска режима «${id}»`),
    [runAndSync],
  )

  const startKind = useCallback(
    (kind: string) =>
      runAndSync(
        () => scenariosApi.startKinds([kind], true),
        `Ошибка запуска сценария «${kind}»`,
      ),
    [runAndSync],
  )

  const stop = useCallback(
    () => runAndSync(() => scenariosApi.stop(), 'Ошибка остановки'),
    [runAndSync],
  )

  const running = status?.running ?? false
  const runningKind = running && status?.kind ? status.kind : null
  const runningTitle =
    modes.find((m) => m.mode === runningKind)?.title ||
    kinds.find((k) => k.kind === runningKind)?.title ||
    runningKind

  const badge = (fault: boolean) => (
    <span className={`rm-badge${fault ? ' rm-badge-fault' : ' rm-badge-ok'}`}>
      {fault ? 'авария' : 'норма'}
    </span>
  )

  const runInfo = runningKind && status ? (
    <div className="rm-runinfo">пуск {formatWhen(status.started_at)} · записано {status.writes}</div>
  ) : null

  return (
    <section className="rm">
      <div className="rm-head">
        <div>
          <h2>Сценарии и режимы</h2>
          <p className="rm-sub">
            Синтетические эпизоды модели и реальные записи режимов — проигрываются в темпе
          </p>
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

      {kinds.length > 0 && (
        <>
          <div className="rm-block-title">Синтетические сценарии (эпизоды)</div>
          <div className="rm-cards">
            {kinds.map((k) => {
              const isRunning = runningKind === k.kind
              return (
                <div key={k.kind} className={`rm-card${isRunning ? ' rm-card-running' : ''}`}>
                  <div className="rm-card-head">
                    <span className="rm-title" title={k.kind}>{k.title}</span>
                    {badge(k.fault)}
                  </div>
                  <div className="rm-meta">
                    <span>модель</span>
                    <span>{k.fault ? 'эпизод аварии' : 'контроль'}</span>
                  </div>
                  <div className="rm-actions">
                    <button
                      type="button"
                      className="rm-run"
                      disabled={!available || busy || running}
                      onClick={() => startKind(k.kind)}
                    >
                      {isRunning ? '…' : 'Запустить'}
                    </button>
                    {isRunning && (
                      <button type="button" className="rm-stop" disabled={busy} onClick={stop}>
                        Стоп
                      </button>
                    )}
                  </div>
                  {isRunning && runInfo}
                </div>
              )
            })}
          </div>
        </>
      )}

      <div className="rm-block-title">Режимы записей телеметрии</div>
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
                {badge(m.fault)}
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
                  onClick={() => startMode(m.mode)}
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
              {isRunning && runInfo}
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