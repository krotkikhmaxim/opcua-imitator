import { useEffect, useMemo, useState } from 'react'
import { archiveApi, playbackApi } from '../../api'
import { ArchiveSeriesResponse, PlaybackStatus } from '../../types'
import './ArchivePlaybackPanel.css'

interface Props {
  onNotify: (kind: 'ok' | 'err', text: string) => void
}

function usToLocalInput(us: number): string {
  const d = new Date(us / 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  )
}

function localInputToUs(value: string): number | null {
  if (!value) return null
  const ms = new Date(value).getTime()
  return Number.isFinite(ms) ? Math.round(ms * 1000) : null
}

/**
 * Загрузка окна SQLite-архива станции в общий плейбэк. Транспорт (пуск,
 * пауза, скорость, переход) — один на оба источника и живёт в
 * PlaybackPanel: здесь только выбор сигналов и окна.
 */
export default function ArchivePlaybackPanel({ onNotify }: Props) {
  const [info, setInfo] = useState<ArchiveSeriesResponse | null>(null)
  const [unavailable, setUnavailable] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string> | null>(null) // null — все
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<PlaybackStatus | null>(null)

  // Общий статус плейбэка (с XLSX-панелью) — только для метки «в плейбэке».
  useEffect(() => {
    const poll = () =>
      playbackApi
        .status()
        .then(setStatus)
        .catch(() => {})
    poll()
    const interval = setInterval(poll, 2000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    archiveApi
      .series()
      .then((res) => {
        setInfo(res)
        if (res.observed_min_us !== null && res.observed_max_us !== null) {
          setStart(usToLocalInput(res.observed_min_us))
          setEnd(usToLocalInput(Math.min(res.observed_max_us, res.observed_min_us + 3_600_000_000)))
        }
      })
      .catch((e) => {
        const detail = e?.response?.data?.detail
        setUnavailable(typeof detail === 'string' ? detail : 'Архив недоступен')
      })
  }, [])

  const signals = info?.series ?? []
  const chosenCount = selected === null ? signals.length : selected.size

  const list = useMemo(() => signals.slice().sort((a, b) => a.signal_id.localeCompare(b.signal_id)), [signals])

  const load = () => {
    const startUs = start ? localInputToUs(start) : null
    const endUs = end ? localInputToUs(end) : null
    if ((start && startUs === null) || (end && endUs === null)) {
      onNotify('err', 'Окно задано неверно')
      return
    }
    setBusy(true)
    playbackApi
      .loadArchive({
        signal_ids: selected === null ? null : Array.from(selected),
        start_us: startUs,
        end_us: endUs,
      })
      .then((next) => {
        onNotify('ok', `Архив загружен: ${next.mapped_signals} сигналов`)
      })
      .catch((e) => {
        onNotify('err', e?.response?.data?.detail || 'Архив не загружен')
      })
      .finally(() => setBusy(false))
  }

  if (unavailable) {
    return (
      <section className="pba">
        <div className="pb-head">
          <h3>Плейбэк архива станции</h3>
        </div>
        <div className="pb-err">{unavailable}</div>
      </section>
    )
  }
  if (!info) return null

  const archiveLoaded =
    status?.loaded === true && (status.source || '').includes('архив')

  return (
    <section className="pba">
      <div className="pb-head">
        <h3>Плейбэк архива станции</h3>
        <span className="pb-meta">
          {info.path} · {info.total_rows.toLocaleString('ru-RU')} отсчётов · {signals.length} сигналов
        </span>
        {archiveLoaded && <span className="pb-phase">в плейбэке</span>}
      </div>

      <div className="pba-window">
        <label>
          с
          <input
            type="datetime-local"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <label>
          по
          <input
            type="datetime-local"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="pb-btn pb-btn-main"
          disabled={busy}
          onClick={load}
        >
          {busy ? 'Загрузка…' : `Загрузить окно${selected === null ? '' : ` (${chosenCount} сигн.)`}`}
        </button>
      </div>

      <div className="pba-signals-head">
        <span className="pb-meta">сигналы: {selected === null ? 'все' : `${chosenCount} из ${signals.length}`}</span>
        <button type="button" className="pba-link" onClick={() => setSelected(null)}>
          все
        </button>
        <button type="button" className="pba-link" onClick={() => setSelected(new Set())}>
          очистить
        </button>
      </div>
      {selected !== null && (
        <div className="pba-signals">
          {list.map((s) => (
            <label key={s.signal_id} className="pba-signal" title={`${s.rows} отсчётов · ${s.value_type}`}>
              <input
                type="checkbox"
                checked={selected.has(s.signal_id)}
                onChange={(e) => {
                  const next = new Set(selected)
                  if (e.target.checked) next.add(s.signal_id)
                  else next.delete(s.signal_id)
                  setSelected(next)
                }}
              />
              {s.signal_id}
            </label>
          ))}
        </div>
      )}
    </section>
  )
}
