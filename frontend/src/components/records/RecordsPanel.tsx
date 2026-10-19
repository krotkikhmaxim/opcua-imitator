import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { machineApi, scenariosApi } from '../../api'
import { RecordedMode, ScenarioStatus } from '../../types'
import './RecordsPanel.css'

const POLL_MS = 1200

function fmt(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function duration(ms: number): string {
  const min = Math.round(ms / 60000)
  if (min >= 1) return `${min} мин`
  return `${Math.max(1, Math.round(ms / 1000))} с`
}

function errorText(e: any, fallback: string): string {
  const detail = e?.response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

/**
 * Проигрыватель записей телеметрии: только воспроизведение, без кнопок
 * внесения аварий — воздействия живут на вкладке «Управление машиной».
 */
export default function RecordsPanel() {
  const [modes, setModes] = useState<RecordedMode[]>([])
  const [modeId, setModeId] = useState<string>('')
  const [status, setStatus] = useState<ScenarioStatus | null>(null)
  const [position, setPosition] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [machineNote, setMachineNote] = useState<string | null>(null)
  const lastTick = useRef<number>(0)

  const mode = useMemo(() => modes.find((m) => m.mode === modeId) ?? null, [modes, modeId])

  const load = useCallback(async () => {
    try {
      const desc = await scenariosApi.describe()
      setModes(desc.modes)
      setStatus(desc.status)
      setModeId((current) => current || desc.modes[0]?.mode || '')
    } catch {
      /* следующая попытка через интервал */
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const playing =
    !!status?.running && status.kind === modeId && status.phase !== 'paused' && !!mode
  const paused = status?.phase === 'paused' && status.kind === modeId
  const total = mode?.duration_ms ?? 0

  useEffect(() => {
    let alive = true
    const tick = async () => {
      if (!alive) return
      try {
        const next = await scenariosApi.status()
        setStatus(next)
        if (next.running && next.kind === modeId && next.phase !== 'paused') {
          const now = Date.now()
          const delta = lastTick.current ? now - lastTick.current : POLL_MS
          lastTick.current = now
          setPosition((pos) => (total > 0 ? Math.min(total, pos + delta) : pos + delta))
        } else if (next.kind === modeId && next.phase === 'finished' && total > 0) {
          lastTick.current = 0
          setPosition(total)
        } else {
          lastTick.current = 0
        }
      } catch {
        /* пропуск опроса */
      }
      try {
        const machine = await machineApi.status()
        setMachineNote(
          machine.phase === 'running' || machine.phase === 'event'
            ? 'Идёт тест машины: записи недоступны, пока тест не завершён.'
            : null,
        )
      } catch {
        setMachineNote(null)
      }
    }
    const interval = setInterval(tick, POLL_MS)
    return () => {
      alive = false
      clearInterval(interval)
    }
  }, [modeId, total])

  const play = useCallback(async () => {
    if (!mode) return
    setBusy(true)
    setError(null)
    setPosition(0)
    lastTick.current = Date.now()
    try {
      setStatus(await scenariosApi.startMode(mode.mode))
    } catch (e: any) {
      setError(errorText(e, 'Ошибка воспроизведения записи'))
    } finally {
      setBusy(false)
    }
  }, [mode])

  const togglePause = useCallback(async () => {
    setBusy(true)
    try {
      setStatus(paused ? await scenariosApi.resume() : await scenariosApi.pause())
      lastTick.current = paused ? Date.now() : 0
    } catch (e: any) {
      setError(errorText(e, 'Ошибка управления воспроизведением'))
    } finally {
      setBusy(false)
    }
  }, [paused])

  const fromStart = useCallback(async () => {
    if (!mode) return
    setBusy(true)
    setError(null)
    try {
      await scenariosApi.stop()
      setPosition(0)
      lastTick.current = Date.now()
      setStatus(await scenariosApi.startMode(mode.mode))
    } catch (e: any) {
      setError(errorText(e, 'Ошибка перезапуска записи'))
    } finally {
      setBusy(false)
    }
  }, [mode])

  const percent = total > 0 ? Math.min(100, (position / total) * 100) : 0
  const disabledByMachine = machineNote !== null

  return (
    <section className="rp">
      <h2 className="rp-title">Записи телеметрии</h2>
      <p className="rp-sub">Записи реальных режимов — воспроизводятся в своём темпе.</p>

      <label className="rp-label" htmlFor="rp-select">
        Выберите запись
      </label>
      <select
        id="rp-select"
        className="rp-select"
        value={modeId}
        onChange={(e) => {
          setModeId(e.target.value)
          setPosition(0)
          setError(null)
        }}
        disabled={playing || busy}
      >
        {modes.map((m) => (
          <option key={m.mode} value={m.mode}>
            {m.title} · {duration(m.duration_ms)}
          </option>
        ))}
        {modes.length === 0 && <option value="">Записей нет</option>}
      </select>

      {mode && (
        <p className="rp-desc">
          <span className="rp-desc-label">Описание:</span> {mode.description || mode.title}
        </p>
      )}

      <div className="rp-actions">
        <button
          type="button"
          className="rp-play"
          disabled={!mode || busy || playing || paused || disabledByMachine}
          onClick={play}
        >
          {busy ? 'Запуск…' : 'Воспроизвести'}
        </button>
        {(playing || paused) && (
          <>
            <button type="button" className="rp-ctl" disabled={busy} onClick={togglePause}>
              {paused ? 'Продолжить' : 'Пауза'}
            </button>
            <button type="button" className="rp-ctl" disabled={busy} onClick={fromStart}>
              С начала
            </button>
          </>
        )}
      </div>

      {machineNote && <div className="rp-note">{machineNote}</div>}
      {error && <div className="rp-error">{error}</div>}

      <div className="rp-timeline">
        <span className="mono rp-time">{fmt(position)}</span>
        <div className="rp-bar">
          <div className="rp-bar-fill" style={{ width: `${percent}%` }} />
        </div>
        <span className="mono rp-time">{fmt(total)}</span>
      </div>

      {status?.error && <div className="rp-error">Ошибка воспроизведения: {status.error}</div>}
    </section>
  )
}
