import { useMemo, useState, MouseEvent as ReactMouseEvent } from 'react'
import { signalsApi } from '../../api'
import { Signal } from '../../types'
import './MnemoPanel.css'

type DirectionFilter = 'all' | 'DI' | 'PIW' | 'Counter'

const DIRECTIONS: DirectionFilter[] = ['all', 'DI', 'PIW', 'Counter']

const DIRECTION_LABEL: Record<DirectionFilter, string> = {
  all: 'Все',
  DI: 'Дискретные',
  PIW: 'Аналоговые',
  Counter: 'Счётчики',
}

interface Props {
  signals: Signal[]
  driverActive: boolean
  showStatus: (kind: 'ok' | 'err', text: string) => void
  /** Сигнал → обозначение аппарата Э3 (мост); включает выбор аппарата кликом. */
  deviceBySignal?: Record<string, string>
  selectedDevice?: string | null
  onSelectDevice?: (dt: string | null) => void
  /** Вызывается после записи (успех или отказ): обновить подтверждённые состояния. */
  onWritten?: () => void
}

function isBool(signal: Signal): boolean {
  return signal.type === 'Boolean' || signal.type === 'bool'
}

function formatValue(signal: Signal): string {
  const v = signal.value
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'boolean') return v ? '1' : '0'
  return String(v)
}

export default function MnemoPanel({
  signals,
  driverActive,
  showStatus,
  deviceBySignal,
  selectedDevice,
  onSelectDevice,
  onWritten,
}: Props) {
  const [direction, setDirection] = useState<DirectionFilter>('all')
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set())
  // Выбран аппарат и показываем весь список (по умолчанию — только его сигналы).
  const [showAll, setShowAll] = useState(false)

  const scoped = useMemo(() => {
    if (!selectedDevice) return signals
    return signals.filter((s) => deviceBySignal?.[s.id] === selectedDevice)
  }, [signals, selectedDevice, deviceBySignal])

  const filtered = useMemo(() => {
    const base = selectedDevice && showAll ? signals : scoped
    return direction === 'all' ? base : base.filter((s) => s.direction === direction)
  }, [scoped, signals, direction, selectedDevice, showAll])

  const groups = useMemo(() => {
    const byCabinet = new Map<string, Signal[]>()
    for (const s of filtered) {
      const key = s.cabinet || '—'
      const arr = byCabinet.get(key) || []
      arr.push(s)
      byCabinet.set(key, arr)
    }
    return Array.from(byCabinet.entries()).sort((a, b) => a[0].localeCompare(b[0]))
  }, [filtered])

  const toggle = async (s: Signal) => {
    if (driverActive || busyIds.has(s.id) || !isBool(s)) return
    const next = !Boolean(s.value)
    setBusyIds((prev) => new Set(prev).add(s.id))
    try {
      // ТЗ #5 §8: команда серверу, а не локальная замена — идемпотентный
      // op_id и ревизия на момент чтения; конфликт вернёт отказ, и состояние
      // запросится заново, а не перезапишется незаметно.
      const res = await signalsApi.writeBatch(
        { [s.id]: next },
        {
          op_id: crypto.randomUUID(),
          expected_revisions:
            typeof s.revision === 'number' ? { [s.id]: s.revision } : undefined,
        },
      )
      if (res.errors.length > 0) {
        showStatus('err', `${s.name}: ${res.errors.join('; ')}`)
        onWritten?.()
      }
    } catch (e: any) {
      showStatus('err', e?.response?.data?.detail || `Ошибка записи «${s.name}»`)
      onWritten?.()
    } finally {
      setBusyIds((prev) => {
        const next2 = new Set(prev)
        next2.delete(s.id)
        return next2
      })
    }
  }

  return (
    <div className="mnemo">
      <div className="mnemo-toolbar">
        <div className="mnemo-tabs">
          {DIRECTIONS.map((d) => (
            <button
              key={d}
              type="button"
              className={`mnemo-tab${direction === d ? ' mnemo-tab-on' : ''}`}
              onClick={() => setDirection(d)}
            >
              {DIRECTION_LABEL[d]}
            </button>
          ))}
        </div>
        {driverActive && (
          <span className="mnemo-driver" title="Ручная запись отключена, пока идёт сценарий или плейбэк">
            Идёт драйвер — тумблеры заблокированы
          </span>
        )}
        {selectedDevice && (
          <span className="mnemo-device-chip">
            Аппарат {selectedDevice} · {scoped.length}
            <button
              type="button"
              className="mnemo-device-chip-btn"
              onClick={() => setShowAll((v) => !v)}
            >
              {showAll ? 'только аппарат' : 'показать все'}
            </button>
            <button
              type="button"
              className="mnemo-device-chip-btn"
              onClick={() => onSelectDevice?.(null)}
              title="Снять выбор аппарата"
            >
              ✕
            </button>
          </span>
        )}
      </div>

      {groups.map(([cabinet, list]) => (
        <div key={cabinet} className="mnemo-group">
          <div className="mnemo-group-head">
            <h3>{cabinet}</h3>
            <span className="mnemo-count">{list.length}</span>
          </div>
          <div className="mnemo-grid">
            {list.map((s) => {
              const on = Boolean(s.value)
              const device = deviceBySignal?.[s.id]
              const nameClick =
                device && onSelectDevice
                  ? (e: ReactMouseEvent) => {
                      e.stopPropagation()
                      onSelectDevice(device === selectedDevice ? null : device)
                    }
                  : undefined
              const nameProps = {
                className: `mnemo-badge-name${device ? ' mnemo-badge-name-link' : ''}`,
                title: device
                  ? `Аппарат ${device} — показать на схеме Э3`
                  : s.logic || s.name,
                onClick: nameClick,
              }
              if (isBool(s)) {
                const toggleable = s.writable
                return (
                  <button
                    key={s.id}
                    type="button"
                    className={`mnemo-badge mnemo-badge-bool ${on ? 'mnemo-on' : 'mnemo-off'}${
                      toggleable && !driverActive ? ' mnemo-toggleable' : ''
                    }`}
                    disabled={!toggleable || driverActive || busyIds.has(s.id)}
                    onClick={() => toggle(s)}
                    title={toggleable && !driverActive ? 'Нажмите, чтобы переключить' : s.logic || s.name}
                  >
                    <span {...nameProps}>{s.name}</span>
                    <span className="mnemo-badge-value">{on ? 'ВКЛ' : 'ОТКЛ'}</span>
                  </button>
                )
              }
              return (
                <div key={s.id} className={`mnemo-badge mnemo-badge-num${on || s.value ? '' : ' mnemo-zero'}`}>
                  <span {...nameProps}>{s.name}</span>
                  <span className="mnemo-badge-value mnemo-mono">{formatValue(s)}</span>
                </div>
              )
            })}
          </div>
        </div>
      ))}
      {groups.length === 0 && <div className="mnemo-empty">Нет сигналов</div>}
    </div>
  )
}
