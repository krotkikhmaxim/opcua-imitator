import { useEffect, useMemo, useState } from 'react'
import { schematicApi } from '../../api'
import { SchematicBridge, Signal, StationSchematicPagesResponse } from '../../types'
import './SchematicView.css'

interface Props {
  signals: Signal[]
  bridge: SchematicBridge
  selectedDevice: string | null
  onSelectDevice: (dt: string | null) => void
}

function isBool(signal: Signal): boolean {
  return signal.type === 'Boolean' || signal.type === 'bool'
}

function valueText(v: Signal['value']): string {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'boolean') return v ? '1' : '0'
  return String(v)
}

/** Лист Э3 как фон: растр станции + бейджи аппаратов моста с живыми значениями.
 *
 * Координаты бейджей — проценты из моста, поэтому позиция верна при любом
 * масштабе растра. Клик по бейджу выбирает аппарат (подсветка его сигналов
 * в MnemoPanel), повторный клик снимает выбор.
 */
export default function SchematicView({ signals, bridge, selectedDevice, onSelectDevice }: Props) {
  const [titles, setTitles] = useState<Record<number, string>>({})
  const [page, setPage] = useState<number | null>(null)
  const [rasterBroken, setRasterBroken] = useState(false)

  const byId = useMemo(() => {
    const map: Record<string, Signal> = {}
    for (const s of signals) map[s.id] = s
    return map
  }, [signals])

  // Листы моста: только те, где есть аппараты стенда.
  const sheets = useMemo(() => {
    const set = new Set<number>()
    for (const dev of Object.values(bridge.devices)) {
      for (const n of Object.keys(dev.sheets)) set.add(Number(n))
    }
    return Array.from(set).sort((a, b) => a - b)
  }, [bridge])

  // Описания листов — из списка станции; список не ответит, останутся номера.
  useEffect(() => {
    let cancelled = false
    schematicApi
      .pages()
      .then((res: StationSchematicPagesResponse) => {
        if (cancelled) return
        const map: Record<number, string> = {}
        for (const p of res.pages) {
          map[p.number] = p.description || p.sheet_designation || ''
        }
        setTitles(map)
      })
      .catch(() => {
        /* номера листов и без описаний */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const currentPage = page !== null && sheets.includes(page) ? page : (sheets[0] ?? null)

  // Выбор из мнемо: если аппарат не на текущем листе — перейти к его первому.
  useEffect(() => {
    if (!selectedDevice) return
    const dev = bridge.devices[selectedDevice]
    if (!dev) return
    const nums = Object.keys(dev.sheets)
      .map(Number)
      .sort((a, b) => a - b)
    if (nums.length > 0 && !nums.includes(currentPage ?? -1)) {
      setPage(nums[0])
    }
    // currentPage намеренно не в зависимостях: переход только при смене выбора.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDevice, bridge])

  useEffect(() => {
    setRasterBroken(false)
  }, [currentPage])

  const badges = useMemo(() => {
    if (currentPage === null) return []
    const list: { dt: string; x: number; y: number; signals: Signal[] }[] = []
    for (const [dt, dev] of Object.entries(bridge.devices)) {
      const point = dev.sheets[String(currentPage)]
      if (!point) continue
      list.push({
        dt,
        x: point[0],
        y: point[1],
        signals: dev.signals.map((id) => byId[id]).filter(Boolean),
      })
    }
    return list
  }, [bridge, currentPage, byId])

  const cov = bridge.coverage

  return (
    <div className="sch">
      <div className="sch-toolbar">
        <span className="sch-title">Схема Э3</span>
        <div className="sch-sheets">
          {sheets.map((n) => (
            <button
              key={n}
              type="button"
              className={`sch-sheet${n === currentPage ? ' sch-sheet-on' : ''}`}
              onClick={() => setPage(n)}
              title={titles[n] || ''}
            >
              {n}
            </button>
          ))}
        </div>
        {currentPage !== null && titles[currentPage] && (
          <span className="sch-sheet-title">{titles[currentPage]}</span>
        )}
      </div>
      <div className="sch-canvas">
        {currentPage === null && <div className="sch-empty">На листах нет аппаратов стенда</div>}
        {currentPage !== null && rasterBroken && (
          <div className="sch-empty">
            Растр листа {currentPage} недоступен (станция отвечает? {currentPage}-й лист с растром?)
          </div>
        )}
        {currentPage !== null && !rasterBroken && (
          <>
            <img
              className="sch-raster"
              src={schematicApi.rasterUrl(currentPage)}
              alt={`Лист ${currentPage}`}
              onError={() => setRasterBroken(true)}
            />
            {badges.map(({ dt, x, y, signals: list }) => (
              <button
                key={dt}
                type="button"
                className={`sch-badge${dt === selectedDevice ? ' sch-badge-selected' : ''}`}
                style={{ left: `${x}%`, top: `${y}%` }}
                onClick={() => onSelectDevice(dt === selectedDevice ? null : dt)}
                title={list.map((s) => `${s.name}: ${valueText(s.value)}`).join('\n')}
              >
                <span className="sch-badge-dt">{dt}</span>
                <span className="sch-badge-values">
                  {list.slice(0, 3).map((s) =>
                    isBool(s) ? (
                      <span
                        key={s.id}
                        className={`sch-sig${s.value ? ' sch-sig-on' : ''}`}
                      />
                    ) : (
                      <span key={s.id} className="sch-sig sch-sig-text">
                        {valueText(s.value)}
                      </span>
                    ),
                  )}
                  {list.length > 3 && (
                    <span className="sch-sig sch-sig-text">+{list.length - 3}</span>
                  )}
                </span>
              </button>
            ))}
          </>
        )}
      </div>
      <div className="sch-coverage">
        мост: {cov.signals_matched}/{cov.signals_total} сигналов · {cov.devices} аппаратов ·{' '}
        {cov.sheets} листов
        {bridge.unmatched_signals.length > 0 &&
          ` · без сопоставления: ${bridge.unmatched_signals.length}`}
      </div>
    </div>
  )
}
