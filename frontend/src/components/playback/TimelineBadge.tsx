import useTimelineStatus from './useTimelineStatus'
import { formatWallTime, parseIsoMs } from './walltime'

const LABELS: Record<string, string> = {
  playing: 'идёт',
  paused: 'пауза',
  finished: 'завершён',
  stopped: 'остановлен',
  error: 'ошибка',
}

/**
 * Компактный бейдж «какое сейчас состояние по таймлайну» — для
 * мнемосхемы и стат-режимов. Клик (если задан onOpen) открывает сам
 * таймлайн. Пока тренд не загружен, бейджа нет.
 */
export default function TimelineBadge({ onOpen }: { onOpen?: () => void }) {
  const status = useTimelineStatus(1000)
  if (!status || !status.loaded) return null

  const label = LABELS[status.phase] ?? status.phase
  const t0Ms = parseIsoMs(status.t0)
  const trendNowMs = t0Ms !== null ? t0Ms + status.position_ms : null

  const body = (
    <>
      Таймлайн: {label}
      {trendNowMs !== null && ` · ${formatWallTime(trendNowMs)}`}
    </>
  )

  if (!onOpen) {
    return <span className={`tl-badge tl-badge-${status.phase}`}>{body}</span>
  }
  return (
    <button
      type="button"
      className={`tl-badge tl-badge-${status.phase}`}
      title="Открыть таймлайн"
      onClick={onOpen}
    >
      {body}
    </button>
  )
}
