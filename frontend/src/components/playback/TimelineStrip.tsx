import { useCallback, useEffect, useRef, useState } from 'react'
import { playbackApi, scenariosApi } from '../../api'
import { ScenarioDescribe, ScenarioStatus } from '../../types'
import useTimelineStatus from './useTimelineStatus'
import { formatDuration, formatWall, parseIsoMs } from './walltime'

/**
 * Полоса таймлайна поверх страницы: при идущем прогоне показывает, что
 * значениями правит тренд (момент тренда, позиция, скорость), а при
 * применении сценария или режима сообщает, что таймлайн остановлен и что
 * применено, и даёт продолжить с места останова. Живёт под шапкой на всех
 * вкладках — оператор видит состояние таймлайна, даже не находясь в нём.
 */
export default function TimelineStrip() {
  const status = useTimelineStatus(1000)
  const [scenario, setScenario] = useState<ScenarioStatus | null>(null)
  const [describe, setDescribe] = useState<ScenarioDescribe | null>(null)
  // Баннер останова: выставляется переходом идёт/пауза → остановлен при
  // работающем сценарии; снимается возобновлением прогона.
  const [halted, setHalted] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const prevPhase = useRef<string | null>(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const next = await scenariosApi.status()
        if (alive) setScenario(next)
      } catch {
        /* сценарный сервис недоступен — держим прошлое */
      }
    }
    void tick()
    const interval = setInterval(tick, 1000)
    return () => {
      alive = false
      clearInterval(interval)
    }
  }, [])

  useEffect(() => {
    scenariosApi
      .describe()
      .then(setDescribe)
      .catch(() => setDescribe(null))
  }, [])

  // Заголовок применённого состояния: kind статуса — id вида (синтетика)
  // или id записанного режима; расшифровка — в describe.
  const titleFor = useCallback(
    (kind: string | null): string | null => {
      if (!kind || !describe) return null
      const k = describe.kinds.find((x) => x.kind === kind)
      if (k) return k.title
      const m = describe.modes.find((x) => x.mode === kind)
      if (m) return m.title
      return kind
    },
    [describe],
  )

  useEffect(() => {
    if (!status) return
    const was = prevPhase.current
    prevPhase.current = status.phase
    const wasActive = was === 'playing' || was === 'paused'
    if (wasActive && status.phase === 'stopped' && scenario?.running) {
      setHalted(titleFor(scenario.kind) ?? 'состояние сценария')
    }
    if (status.phase !== 'stopped') setHalted(null)
  }, [status, scenario, titleFor])

  const resume = useCallback(async () => {
    if (!status) return
    setBusy(true)
    try {
      await playbackApi.control('play', {
        start_ms: status.position_ms,
        end_ms: status.end_ms ?? undefined,
      })
      setHalted(null)
    } catch {
      /* полоса не тостит — состояние покажет следующий опрос */
    } finally {
      setBusy(false)
    }
  }, [status])

  if (!status || !status.loaded) return null

  const t0Ms = parseIsoMs(status.t0)
  const trendNowMs = t0Ms !== null ? t0Ms + status.position_ms : null
  const active = status.phase === 'playing' || status.phase === 'paused'
  const onWindow = status.start_ms > 0 || status.end_ms !== null
  // Заморозка таймлайна (ТЗ #5): полоса остаётся видимой, но помечена как
  // отключённая от управления, «Продолжить» не даётся.
  const frozen = status.writes_enabled === false

  return (
    <div className={`tl-strip${active ? ' tl-strip-run' : ' tl-strip-idle'}`}>
      <span className="tl-strip-dot" aria-hidden />
      {active ? (
        <span className="tl-strip-text">
          Таймлайн{status.paused ? ' — пауза' : ' — идёт'} · момент тренда{' '}
          <b>{trendNowMs !== null ? formatWall(trendNowMs) : '—'}</b> · {formatDuration(status.position_ms)}
          {status.duration_ms !== null && ` / ${formatDuration(status.duration_ms)}`}
          {onWindow &&
            ` · участок ${formatDuration(status.start_ms)}–${
              status.end_ms !== null ? formatDuration(status.end_ms) : '…'
            }`}
          {' · '}
          {status.rate}×
        </span>
      ) : (
        <span className="tl-strip-text">
          Таймлайн — {status.phase === 'finished' ? 'завершён' : 'остановлен'} · позиция{' '}
          {formatDuration(status.position_ms)}
          {status.duration_ms !== null && ` / ${formatDuration(status.duration_ms)}`}
        </span>
      )}

      {frozen && <span className="tl-strip-text">· отключён от управления сервером</span>}

      {halted && (
        <span className="tl-strip-banner">
          Таймлайн остановлен — применено: {halted}
          {!frozen && (
            <button
              type="button"
              className="tl-strip-btn"
              disabled={busy || Boolean(scenario?.running)}
              title={scenario?.running ? 'Сначала остановите сценарий' : 'Продолжить с места останова'}
              onClick={resume}
            >
              ▶ Продолжить
            </button>
          )}
          <button
            type="button"
            className="tl-strip-x"
            title="Скрыть"
            onClick={() => setHalted(null)}
          >
            ×
          </button>
        </span>
      )}
    </div>
  )
}
