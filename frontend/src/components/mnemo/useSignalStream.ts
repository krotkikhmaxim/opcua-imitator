import { useEffect, useRef, useState } from 'react'

/** Дельта одного сигнала (ТЗ #5 §12): значение, ревизия, источник. */
export interface SignalEntry {
  v: unknown
  r: number
  s?: string | null
}

/** Новый владелец сигнала (или null при освобождении). */
export type OwnerEntry = { id: string; title: string } | null

export interface SignalStream {
  connected: boolean
  activeDrivers: string[]
}

interface SignalsSnapshotMessage {
  type: 'signals_snapshot'
  seq: number
  signals: Record<string, Record<string, unknown>>
}

interface SignalsUpdateMessage {
  type: 'signals_update'
  seq: number
  changes: Record<string, SignalEntry>
  owners?: Record<string, OwnerEntry>
}

interface DriverChangedMessage {
  type: 'driver_changed'
  active: string[]
}

type FeedMessage = SignalsSnapshotMessage | SignalsUpdateMessage | DriverChangedMessage

interface UseSignalStreamOptions {
  /** Применить дельту значений (с ревизиями) и, при передаче, карту владельцев. */
  onSignals: (changes: Record<string, SignalEntry>, owners?: Record<string, OwnerEntry>) => void
  /** Заменить состояние полным снимком (при подключении). */
  onSnapshot: (signals: Record<string, Record<string, unknown>>) => void
  /** Обнаружен пропуск seq — часть дельт потеряна, нужен свежий снимок. */
  onGap: () => void
}

/**
 * Подписка на снимок, дельты с ревизиями и смены драйвера (WS /ws).
 *
 * §12: при подключении сервер сначала присылает снимок, затем дельты с
 * монотонным seq. Пропуск seq (потерянные сообщения) — повторный запрос
 * снимка через onGap. Переподключение с нарастающей задержкой до 10 с;
 * пока связи нет, App блокирует управление и помечает значения устаревшими.
 */
export function useSignalStream(options: UseSignalStreamOptions): SignalStream {
  const [connected, setConnected] = useState(false)
  const [activeDrivers, setActiveDrivers] = useState<string[]>([])
  const handlers = useRef(options)
  handlers.current = options

  useEffect(() => {
    let ws: WebSocket | null = null
    let disposed = false
    let attempt = 0
    let timer: number | undefined
    let lastSeq: number | null = null

    const connect = () => {
      if (disposed) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${window.location.host}/ws`)
      ws.onopen = () => {
        setConnected(true)
        attempt = 0
        // До первого сообщения нового соединения ничего не знаем о seq.
        lastSeq = null
      }
      ws.onmessage = (event: MessageEvent) => {
        let message: FeedMessage
        try {
          message = JSON.parse(event.data) as FeedMessage
        } catch {
          return
        }
        if (message.type === 'signals_snapshot') {
          lastSeq = message.seq
          handlers.current.onSnapshot(message.signals)
        } else if (message.type === 'signals_update') {
          if (lastSeq !== null && message.seq > lastSeq + 1) {
            handlers.current.onGap()
          }
          lastSeq = message.seq
          handlers.current.onSignals(message.changes, message.owners)
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
