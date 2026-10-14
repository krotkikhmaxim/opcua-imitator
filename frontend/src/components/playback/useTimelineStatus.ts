import { useEffect, useState } from 'react'
import { playbackApi } from '../../api'
import { PlaybackStatus } from '../../types'

/**
 * Статус плейбэка с опросом раз в секунду. Общий источник для полосы
 * таймлайна, бейджей на мнемосхеме и в режимах — каждый монтирует свой
 * опрос, бэкенд отдаёт статус без побочных эффектов.
 */
export default function useTimelineStatus(pollMs = 1000): PlaybackStatus | null {
  const [status, setStatus] = useState<PlaybackStatus | null>(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const next = await playbackApi.status()
        if (alive) setStatus(next)
      } catch {
        /* бэкенд недоступен — держим последнее известное */
      }
    }
    void tick()
    const interval = setInterval(tick, pollMs)
    return () => {
      alive = false
      clearInterval(interval)
    }
  }, [pollMs])

  return status
}
