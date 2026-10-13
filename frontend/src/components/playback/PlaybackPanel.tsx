import { useCallback, useEffect, useRef, useState } from 'react'
import { playbackApi } from '../../api'
import { PlaybackStatus } from '../../types'
import './PlaybackPanel.css'

const STATUS_POLL_MS = 1000

const RATES = [0.25, 0.5, 1, 2, 4, 8]

function formatMs(ms: number): string {
  const total = Math.floor(ms / 1000)
  const m = Math.floor(total / 60)
  const s = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${m}:${pad(s)}`
}

interface Props {
  onNotify: (kind: 'ok' | 'err', text: string) => void
}

/**
 * Плейбэк XLSX-трендов: файл по пути (большие) или загрузкой (маленькие),
 * транспорт play/pause/seek/скорость. Значения уходят в шину с метками
 * момента тренда; пока идёт плейбэк, ручная запись заблокирована (409).
 */
export default function PlaybackPanel({ onNotify }: Props) {
  const [status, setStatus] = useState<PlaybackStatus | null>(null)
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const refresh = useCallback(async () => {
    try {
      setStatus(await playbackApi.status())
    } catch {
      /* backend недоступен — оставляем прошлое */
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, STATUS_POLL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  const run = useCallback(
    async (action: () => Promise<PlaybackStatus>, okText?: string) => {
      setBusy(true)
      try {
        const next = await action()
        setStatus(next)
        if (okText) onNotify('ok', okText)
      } catch (e: any) {
        onNotify('err', e?.response?.data?.detail || 'Ошибка плейбэка')
        refresh()
      } finally {
        setBusy(false)
      }
    },
    [onNotify, refresh],
  )

  const loadByPath = () => {
    const trimmed = path.trim()
    if (!trimmed) return
    void run(() => playbackApi.loadPath(trimmed))
  }

  const upload = (file: File) => {
    if (file.size > 50 * 1024 * 1024) {
      onNotify('err', 'Файл больше 50 МБ — используйте загрузку по пути')
      return
    }
    void run(() => playbackApi.upload(file))
  }

  const seekSeconds = (seconds: string) => {
    const ms = Math.round(parseFloat(seconds) * 1000)
    if (!Number.isFinite(ms)) return
    void run(() => playbackApi.control('seek', { seek_ms: ms }))
  }

  if (!status) {
    return null
  }

  const phase = status.paused
    ? 'пауза'
    : ({ playing: 'идёт', finished: 'завершён', stopped: 'остановлен', error: 'ошибка' } as Record<string, string>)[
        status.phase
      ] || status.phase

  return (
    <section className="pb">
      <div className="pb-head">
        <h3>Плейбэк трендов</h3>
        {status.loaded && (
          <span className={`pb-phase pb-phase-${status.phase}${status.paused ? ' pb-phase-paused' : ''}`}>
            {phase}
          </span>
        )}
      </div>

      <div className="pb-load">
        <input
          className="pb-path"
          placeholder="Путь к XLSX на сервере (например /data/trend.xlsx)"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && loadByPath()}
        />
        <button type="button" disabled={busy || !path.trim()} onClick={loadByPath}>
          Открыть по пути
        </button>
        <input
          ref={fileInput}
          type="file"
          accept=".xlsx"
          style={{ display: 'none' }}
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) upload(f)
            e.target.value = ''
          }}
        />
        <button type="button" disabled={busy} onClick={() => fileInput.current?.click()}>
          Загрузить файл
        </button>
      </div>

      {status.loaded && (
        <>
          <div className="pb-info">
            <span className="pb-source" title={status.source || ''}>{status.source}</span>
            <span className="pb-meta">
              сигналов {status.mapped_signals}
              {status.unmatched_columns > 0 && ` · вне биндингов ${status.unmatched_columns}`}
              {status.ambiguous > 0 && ` · неоднозначных ${status.ambiguous}`}
            </span>
            <span className="pb-meta">
              {formatMs(status.position_ms)}
              {status.duration_ms !== null && ` / ${formatMs(status.duration_ms)}`}
              {` · записей ${status.writes}`}
              {status.write_failures > 0 && ` · отказов ${status.write_failures}`}
              {status.max_lag_ms > 0 && ` · лаг ${status.max_lag_ms} мс`}
            </span>
          </div>
          {status.error && <div className="pb-err">Ошибка прогона: {status.error}</div>}

          <div className="pb-transport">
            {status.phase !== 'playing' || status.paused ? (
              <button
                type="button"
                className="pb-btn pb-btn-main"
                disabled={busy}
                onClick={() =>
                  run(() => playbackApi.control(status.paused ? 'resume' : 'play'))
                }
              >
                {status.paused ? '▶ Продолжить' : '▶ Пуск'}
              </button>
            ) : (
              <button
                type="button"
                className="pb-btn"
                disabled={busy}
                onClick={() => run(() => playbackApi.control('pause'))}
              >
                ‖ Пауза
              </button>
            )}
            <button
              type="button"
              className="pb-btn"
              disabled={busy || status.phase === 'idle' || status.phase === 'stopped'}
              onClick={() => run(() => playbackApi.control('stop'))}
            >
              ■ Стоп
            </button>
            <label className="pb-rate">
              скорость
              <select
                value={status.rate}
                disabled={busy}
                onChange={(e) =>
                  run(() => playbackApi.control('rate', { rate: parseFloat(e.target.value) }))
                }
              >
                {RATES.map((r) => (
                  <option key={r} value={r}>{r}×</option>
                ))}
              </select>
            </label>
            <label className="pb-seek">
              перейти, мин
              <input
                type="number"
                min={0}
                step={1}
                placeholder="0"
                disabled={busy}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') seekSeconds((e.target as HTMLInputElement).value)
                }}
              />
            </label>
            <button
              type="button"
              className="pb-btn pb-btn-ghost"
              disabled={busy}
              onClick={() => run(() => playbackApi.unload())}
            >
              Выгрузить
            </button>
          </div>
        </>
      )}
    </section>
  )
}
