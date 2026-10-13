import { useEffect, useRef, useState } from 'react'

export interface SignalStream {
  connected: boolean
  activeDrivers: string[]
}

interface SignalsUpdateMessage {
  type: 'signals_update'
  changes: Record<string, unknown>
}

interface DriverChangedMessage {
  type: 'driver_changed'
  active: string[]
}

type FeedMessage = SignalsUpdateMessage | DriverChangedMessage

/**
 * Подписка на дельты значений и смен драйвера (WS /ws).
 *
 * Переподключение с нарастающей задержкой до 10 с; пока связи нет, мнемо
 * живёт на опросе REST, который ведёт App — фолбэк не требует отдельной
 * логики. Активные драйверы сбрасываются при обрыве: без подтверждения
 * сервера нельзя считать, что сценарий ещё идёт.
 */
export function useSignalStream(
  onSignals: (changes: Record<string, unknown>) => void,
): SignalStream {
  const [connected, setConnected] = useState(false)
  const [activeDrivers, setActiveDrivers] = useState<string[]>([])
  const onSignalsRef = useRef(onSignals)
  onSignalsRef.current = onSignals

  useEffect(() => {
    let ws: WebSocket | null = null
    let disposed = false
    let attempt = 0
    let timer: number | undefined

    const connect = () => {
      if (disposed) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${window.location.host}/ws`)
      ws.onopen = () => {
        setConnected(true)
        attempt = 0
      }
      ws.onmessage = (event: MessageEvent) => {
        let message: FeedMessage
        try {
          message = JSON.parse(event.data) as FeedMessage
        } catch {
          return
        }
        if (message.type === 'signals_update') {
          onSignalsRef.current(message.changes)
        } else if (message.type === 'driver_changed') {
          setActiveDrivers(message.active)
        }
      }
      ws.onclose = () => {
        setConnected(false)
        setActiveDrivers([])
        if (!disposed) {
          attempt += 1
          const delay = Math.min(1000 * 2 ** Math.min(attempt - 1, 4), 10_000)
          timer = window.setTimeout(connect, delay)
        }
      }
      ws.onerror = () => ws?.close()
    }

    connect()
    return () => {
      disposed = true
      if (timer !== undefined) window.clearTimeout(timer)
      ws?.close()
    }
  }, [])

  return { connected, activeDrivers }
}
