import { useCallback, useEffect, useRef, useState } from 'react'
import { playbackApi } from '../../api'
import { PlaybackStatus } from '../../types'
import './TimelinePanel.css'

const RATES = [0.25, 0.5, 1, 2, 4, 8]
const PATH_STORAGE_KEY = 'timeline.xlsx.path'
// Тренд по умолчанию лежит в репозитории стенда; строка пути спрятана
// в настройках панели.
const DEFAULT_PATH = '/home/max/opcua-imitator/data/trends/PS_NSH2.xlsx'
const STATUS_POLL_MS = 1000

/** ISO (включая микросекунды) → epoch ms; наивные метки — локальным временем. */
function parseIsoMs(iso: string | null | undefined): number | null {
  if (!iso) return null
  const trimmed = iso.replace(/\.(\d{3})\d+/, '.$1')
  const ms = new Date(trimmed).getTime()
  return Number.isFinite(ms) ? ms : null
}

function formatWall(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}

function formatDuration(ms: number): string {
  const total = Math.floor(ms / 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${Math.floor(total / 3600)}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`
}

function datetimeLocalToMs(value: string): number | null {
  if (!value) return null
  const ms = new Date(value).getTime()
  return Number.isFinite(ms) ? ms : null
}

/**
 * Таймлайн тренда: тот же плейбэк, что и панель в мнемо, но с осью
 * реального времени. Начало тренда t0 известно сразу после загрузки,
 * полная длительность — по мере прогона (ради неё файл целиком не
 * сканируется). Перемотка: полосой, когда длительность известна, или
 * переходом на момент тренда по времени суток (скан с начала файла —
 * семантика seek сервиса). Тренд по умолчанию (файл из репозитория
 * стенда) подгружается сам; путь к другому файлу — в «Настройках».
 */
export default function TimelinePanel({
  onNotify,
}: {
  onNotify: (kind: 'ok' | 'err', text: string) => void
}) {
  const [status, setStatus] = useState<PlaybackStatus | null>(null)
  const [path, setPath] = useState(() => {
    try {
      return localStorage.getItem(PATH_STORAGE_KEY) || DEFAULT_PATH
    } catch {
      return DEFAULT_PATH
    }
  })
  const [busy, setBusy] = useState(false)
  const [scrub, setScrub] = useState<number | null>(null)
  const [jumpTo, setJumpTo] = useState('')
  // Путь к XLSX на сервере — служебная настройка, по умолчанию скрыта.
  const [showSettings, setShowSettings] = useState(false)
  // Автозагрузка тренда по умолчанию — один раз за монтаж панели.
  const autoTried = useRef(false)

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

  // Пока ничего не загружено, сами открываем тренд по умолчанию из
  // репозитория — панель работает без ввода пути.
  useEffect(() => {
    if (!status || status.loaded || autoTried.current) return
    autoTried.current = true
    void run(() => playbackApi.loadPath(DEFAULT_PATH), 'Тренд по умолчанию загружен')
  }, [status, run])

  if (!status) return null

  const t0Ms = parseIsoMs(status.t0)
  const duration = status.duration_ms
  const position = Math.min(status.position_ms, duration ?? status.position_ms)
  const trendNowMs = t0Ms !== null ? t0Ms + status.position_ms : null
  const running = status.phase === 'playing' || status.phase === 'paused'

  const loadByPath = () => {
    const trimmed = path.trim()
    if (!trimmed) return
    void run(async () => {
      const next = await playbackApi.loadPath(trimmed)
      try {
        localStorage.setItem(PATH_STORAGE_KEY, trimmed)
      } catch {
        /* приватный режим — путь просто не запоминаем */
      }
      return next
    }, 'Тренд загружен')
  }

  const commitScrub = () => {
    if (scrub === null) return
    const target = scrub
    setScrub(null)
    void run(() => playbackApi.control('seek', { seek_ms: target }))
  }

  const jump = () => {
    if (t0Ms === null) {
      onNotify('err', 'Начало тренда неизвестно — загрузите файл')
      return
    }
    const wallMs = datetimeLocalToMs(jumpTo)
    if (wallMs === null) {
      onNotify('err', 'Задайте момент тренда')
      return
    }
    const targetMs = Math.round(wallMs - t0Ms)
    if (targetMs < 0) {
      onNotify('err', 'Момент раньше начала тренда')
      return
    }
    const go = () =>
      running
        ? playbackApi.control('seek', { seek_ms: targetMs })
        : playbackApi
            .control('play')
            .then(() => playbackApi.control('seek', { seek_ms: targetMs }))
    void run(go)
  }

  const phase = status.paused
    ? 'пауза'
    : ({ playing: 'идёт', finished: 'завершён', stopped: 'остановлен', error: 'ошибка' } as Record<
        string,
        string
      >)[status.phase] || status.phase

  return (
    <section className="tl">
      <div className="tl-head">
        <h2>Таймлайн тренда</h2>
        {status.loaded && (
          <span
            className={`tl-phase tl-phase-${status.phase}${status.paused ? ' tl-phase-paused' : ''}`}
          >
            {phase}
          </span>
        )}
        <button
          type="button"
          className="tl-settings"
          onClick={() => setShowSettings((v) => !v)}
          title="Путь к XLSX на сервере"
        >
          ⚙ Настройки
        </button>
      </div>

      {showSettings && (
        <div className="tl-load">
          <input
            className="tl-path"
            placeholder="Путь к XLSX на сервере"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && loadByPath()}
          />
          <button
            type="button"
            className="tl-btn tl-btn-main"
            disabled={busy || !path.trim()}
            onClick={loadByPath}
          >
            Открыть
          </button>
        </div>
      )}

      {!status.loaded && (
        <p className="tl-hint">
          Загрузите тренд — таймлайн покажет реальное время записи и даст перемотку.
        </p>
      )}

      {status.loaded && (
        <>
          <div className="tl-meta">
            <span className="tl-source" title={status.source || ''}>
              {status.source}
            </span>
            <span>
              сигналов {status.mapped_signals}
              {status.unmatched_columns > 0 && ` · вне биндингов ${status.unmatched_columns}`}
              {status.ambiguous > 0 && ` · неоднозначных ${status.ambiguous}`}
            </span>
            <span>
              записей {status.writes}
              {status.write_failures > 0 && ` · отказов ${status.write_failures}`}
              {status.max_lag_ms > 0 && ` · лаг ${status.max_lag_ms} мс`}
            </span>
          </div>
          {status.error && <div className="tl-err">Ошибка прогона: {status.error}</div>}

          <div className="tl-clock">
            <span className="tl-clock-value">
              {trendNowMs !== null ? formatWall(trendNowMs) : '—'}
            </span>
            <span className="tl-clock-label">момент тренда</span>
          </div>

          {duration !== null && duration > 0 ? (
            <div className="tl-axis">
              <input
                type="range"
                className="tl-range"
                min={0}
                max={duration}
                step={500}
                disabled={busy || !running}
                value={scrub ?? position}
                onChange={(e) => setScrub(Number(e.target.value))}
                onPointerUp={commitScrub}
                onKeyUp={(e) => e.key === 'Enter' && commitScrub()}
              />
              <div className="tl-axis-labels">
                <span>{t0Ms !== null ? formatWall(t0Ms) : '0:00'}</span>
                <span>
                  {formatDuration(scrub ?? position)} / {formatDuration(duration)}
                </span>
                <span>{t0Ms !== null ? formatWall(t0Ms + duration) : formatDuration(duration)}</span>
              </div>
            </div>
          ) : (
            <p className="tl-hint">
              Полная длительность станет известна при прогоне; пока перематывайте
              «моментом тренда» ниже.
            </p>
          )}

          <div className="tl-transport">
            {status.phase !== 'playing' || status.paused ? (
              <button
                type="button"
                className="tl-btn tl-btn-main"
                disabled={busy}
                onClick={() => run(() => playbackApi.control(status.paused ? 'resume' : 'play'))}
              >
                {status.paused ? '▶ Продолжить' : '▶ Пуск'}
              </button>
            ) : (
              <button
                type="button"
                className="tl-btn"
                disabled={busy}
                onClick={() => run(() => playbackApi.control('pause'))}
              >
                ‖ Пауза
              </button>
            )}
            <button
              type="button"
              className="tl-btn"
              disabled={busy || !running}
              onClick={() => run(() => playbackApi.control('stop'))}
            >
              ■ Стоп
            </button>
            <label className="tl-rate">
              скорость
              <select
                value={status.rate}
                disabled={busy || !running}
                onChange={(e) =>
                  run(() => playbackApi.control('rate', { rate: parseFloat(e.target.value) }))
                }
              >
                {RATES.map((r) => (
                  <option key={r} value={r}>
                    {r}×
                  </option>
                ))}
              </select>
            </label>
            <label className="tl-jump">
              на момент тренда
              <input
                type="datetime-local"
                step={1}
                value={jumpTo}
                onChange={(e) => setJumpTo(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="tl-btn"
              disabled={busy || t0Ms === null || !jumpTo}
              onClick={jump}
            >
              Перейти
            </button>
            <button
              type="button"
              className="tl-btn tl-btn-ghost"
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
