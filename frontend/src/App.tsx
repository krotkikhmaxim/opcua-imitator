import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import StateManager from './components/StateManager'
import { heartbeatApi, signalsApi } from './api'
import { HeartbeatResponse, Signal } from './types'
import './App.css'

interface SignalsResponse {
  signals: Record<string, Omit<Signal, 'id'> & { id?: string }>
}

type DraftValue = string | boolean

const BOOL_TYPES = new Set(['Boolean', 'bool'])
const NUM_TYPES = new Set(['Int16', 'UInt16', 'int', 'float'])

function isBool(signal: Signal): boolean {
  return BOOL_TYPES.has(signal.type)
}

function isNumeric(signal: Signal): boolean {
  return NUM_TYPES.has(signal.type)
}

function signalAddress(s: Signal): string {
  return s.address || s.project_tag || s.id
}

function sortSignals(list: Signal[]): Signal[] {
  return list.slice().sort((a, b) => {
    const cab = (a.cabinet || '').localeCompare(b.cabinet || '')
    if (cab !== 0) return cab
    const mod = (a.module || '').localeCompare(b.module || '', undefined, { numeric: true })
    if (mod !== 0) return mod
    const bitA = a.direction === 'PIW' ? a.word_address : a.bit
    const bitB = b.direction === 'PIW' ? b.word_address : b.bit
    const na = typeof bitA === 'number' ? bitA : parseInt(String(bitA ?? ''), 10)
    const nb = typeof bitB === 'number' ? bitB : parseInt(String(bitB ?? ''), 10)
    const fa = Number.isFinite(na) ? na : Infinity
    const fb = Number.isFinite(nb) ? nb : Infinity
    if (fa !== fb) return fa - fb
    return (a.device || '').localeCompare(b.device || '')
  })
}

export default function App() {
  const [signals, setSignals] = useState<Signal[]>([])
  const [updatedAt, setUpdatedAt] = useState<string>('')
  const [filter, setFilter] = useState('')
  const [status, setStatus] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [drafts, setDrafts] = useState<Record<string, DraftValue>>({})
  const [applying, setApplying] = useState(false)
  const [heartbeat, setHeartbeat] = useState<HeartbeatResponse | null>(null)
  const [heartbeatBusy, setHeartbeatBusy] = useState(false)
  const statusTimer = useRef<number | null>(null)

  const refreshSignals = useCallback(async () => {
    try {
      const res = await fetch('/api/signals').then((r): Promise<SignalsResponse> => r.json())
      const list: Signal[] = Object.entries(res.signals).map(([id, s]) => ({
        id,
        short_id: s.short_id,
        name: s.name,
        type: s.type,
        writable: s.writable,
        value: s.value,
        direction: s.direction,
        channel: s.channel,
        module: s.module,
        cabinet: s.cabinet,
        device: s.device,
        address: s.address,
        project_tag: s.project_tag,
        bit: s.bit,
        word_address: s.word_address,
        logic: s.logic,
      }))
      setSignals(list)
      setUpdatedAt(new Date().toLocaleTimeString())
    } catch {
      // ignore
    }
  }, [])

  useEffect(() => {
    refreshSignals()
    const interval = setInterval(refreshSignals, 5000)
    return () => clearInterval(interval)
  }, [refreshSignals])

  const handleStateLoaded = useCallback(() => {
    refreshSignals()
  }, [refreshSignals])

  const showStatus = useCallback((kind: 'ok' | 'err', text: string) => {
    setStatus({ kind, text })
    if (statusTimer.current) window.clearTimeout(statusTimer.current)
    statusTimer.current = window.setTimeout(() => setStatus(null), 5000)
  }, [])

  // Состояние heartbeat инициализируется из backend, а не хранится локально
  useEffect(() => {
    heartbeatApi
      .get()
      .then(setHeartbeat)
      .catch(() => setHeartbeat({ enabled: false, supported: false }))
  }, [])

  const toggleHeartbeat = useCallback(async () => {
    if (!heartbeat || heartbeatBusy) return
    setHeartbeatBusy(true)
    const target = !heartbeat.enabled
    try {
      const res = await heartbeatApi.set(target)
      setHeartbeat(res)
      showStatus('ok', `Heartbeat: ${res.enabled ? 'включён' : 'выключен'}`)
    } catch (e: any) {
      showStatus('err', e?.response?.data?.detail || 'Ошибка переключения heartbeat')
      try {
        // вернуть UI к фактическому состоянию backend
        const res = await heartbeatApi.get()
        setHeartbeat(res)
      } catch {
        /* оставляем последнее известное состояние */
      }
    } finally {
      setHeartbeatBusy(false)
    }
  }, [heartbeat, heartbeatBusy, showStatus])

  const setDraft = useCallback((id: string, value: DraftValue) => {
    setDrafts((prev) => ({ ...prev, [id]: value }))
  }, [])

  const resetDrafts = useCallback((ids: string[]) => {
    setDrafts((prev) => {
      const next = { ...prev }
      for (const id of ids) delete next[id]
      return next
    })
  }, [])

  const applyChanges = useCallback(async () => {
    const values: Record<string, boolean | number | string> = {}
    for (const id of Object.keys(drafts)) {
      const cfg = signalsById.current[id]
      if (!cfg || !cfg.writable) continue
      values[id] = draftToValue(cfg, drafts[id])
    }
    const ids = Object.keys(values)
    if (ids.length === 0) {
      showStatus('err', 'Нет изменений для применения')
      return
    }
    setApplying(true)
    try {
      const res = await signalsApi.writeBatch(values)
      if (res.errors.length > 0) {
        showStatus('err', `Применено ${res.applied.length} из ${ids.length}. Ошибки: ${res.errors.join('; ')}`)
      } else {
        showStatus('ok', `Применено значений: ${res.applied.length}`)
      }
      resetDrafts(res.applied)
      refreshSignals()
    } catch (e: any) {
      showStatus('err', e?.response?.data?.detail || 'Ошибка применения значений')
    } finally {
      setApplying(false)
    }
  }, [drafts, refreshSignals, resetDrafts, showStatus])

  const signalsById = useRef<Record<string, Signal>>({})
  signalsById.current = useMemo(() => {
    const map: Record<string, Signal> = {}
    for (const s of signals) map[s.id] = s
    return map
  }, [signals])

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return signals
    return signals.filter((s) =>
      [s.name, s.id, s.short_id, s.address, s.project_tag, s.device, s.module, s.channel, s.direction]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q)) ||
      String(s.bit ?? '').toLowerCase().includes(q) ||
      String(s.word_address ?? '').toLowerCase().includes(q),
    )
  }, [signals, filter])

  const groups = useMemo(() => {
    const byChannel = new Map<string, Signal[]>()
    for (const s of filtered) {
      const key = s.channel || '—'
      const arr = byChannel.get(key) || []
      arr.push(s)
      byChannel.set(key, arr)
    }
    return Array.from(byChannel.entries())
      .map(([channel, list]) => [channel, sortSignals(list)] as const)
      .sort((a, b) => a[0].localeCompare(b[0]))
  }, [filtered])

  const changedCount = Object.keys(drafts).filter((id) => signalsById.current[id]?.writable).length

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-row">
          <div>
            <h1>ОПК UA — Мониторинг подъёмной машины</h1>
            <p className="app-subtitle">Входные сигналы ПЛК и слова PROFIBUS · сохранение и загрузка состояний</p>
          </div>
          {heartbeat && heartbeat.supported && (
            <button
              type="button"
              className={`heartbeat-toggle${heartbeat.enabled ? ' heartbeat-toggle-on' : ''}`}
              disabled={heartbeatBusy}
              onClick={toggleHeartbeat}
              title="Heartbeat: циклическая перезапись значений с обновлением SourceTimestamp (1 раз/сек)"
            >
              Heartbeat: {heartbeatBusy ? '…' : heartbeat.enabled ? 'ON' : 'OFF'}
            </button>
          )}
        </div>
      </header>

      <StateManager onStateLoaded={handleStateLoaded} />

      <section className="signals-panel">
        <div className="signals-head">
          <h2>Входные сигналы</h2>
          {updatedAt && <span className="signals-updated">обновлено {updatedAt}</span>}
          <input
            className="signals-filter"
            placeholder="Поиск: название, тег, канал, модуль, устройство, бит…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <span className="signals-count">{filtered.length} из {signals.length}</span>
        </div>
        {status && <div className={`signals-status signals-status-${status.kind}`}>{status.text}</div>}
        <div className="signals-applybar">
          <span className="signals-changed">
            {changedCount > 0 ? `Изменено сигналов: ${changedCount}` : 'Изменений нет'}
          </span>
          <button
            className="signals-apply"
            disabled={applying || changedCount === 0}
            onClick={applyChanges}
          >
            {applying ? 'Применение…' : `Применить${changedCount > 0 ? ` (${changedCount})` : ''}`}
          </button>
          {changedCount > 0 && (
            <button className="signals-cancel" disabled={applying} onClick={() => resetDrafts(Object.keys(drafts))}>
              Сбросить
            </button>
          )}
        </div>
        {groups.map(([channel, list]) => (
          <div className="signals-group" key={channel}>
            <div className="signals-group-head">
              <h3 className="signals-group-title">{channel}</h3>
              <span className="signals-count">{list.length}</span>
            </div>
            <div className="signals-table-wrap">
              <table className="signals-table">
                <thead>
                  <tr>
                    <th>Шкаф</th>
                    <th>Модуль</th>
                    <th>Устройство</th>
                    <th>Бит/Слово</th>
                    <th>Сигнал</th>
                    <th>Тип</th>
                    <th>Значение</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((s) => (
                    <tr key={s.id} className={drafts[s.id] !== undefined ? 'signals-row-changed' : ''}>
                      <td className="signals-cell-cab">{s.cabinet || '—'}</td>
                      <td className="mono">{s.module || '—'}</td>
                      <td className="mono">{s.device || '—'}</td>
                      <td className="mono">
                        <span title={s.direction || ''}>{locationCell(s)}</span>
                      </td>
                      <td>
                        <div className="signals-name" title={s.logic || signalAddress(s)}>
                          {s.name}
                        </div>
                        <div className="signals-tag mono" title={signalAddress(s)}>
                          {signalAddress(s)}
                        </div>
                      </td>
                      <td className="mono">{s.type}</td>
                      <td>
                        <SignalValueCell
                          signal={s}
                          draft={drafts[s.id] !== undefined ? drafts[s.id] : undefined}
                          onChange={setDraft}
                        />
                      </td>
                    </tr>
                  ))}
                  {list.length === 0 && (
                    <tr>
                      <td colSpan={7} className="signals-empty">Нет сигналов</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        ))}
        {groups.length === 0 && (
          <div className="signals-table-wrap">
            <div className="signals-empty">Нет сигналов</div>
          </div>
        )}
      </section>
    </div>
  )
}

function locationCell(s: Signal): string {
  if (s.direction === 'PIW' && s.word_address !== undefined && s.word_address !== null) {
    return `сл. ${s.word_address}`
  }
  if (s.direction === 'Counter') return 'имп.'
  if (s.bit !== undefined && s.bit !== null) return `бит ${s.bit}`
  return '—'
}

interface CellProps {
  signal: Signal
  draft: DraftValue | undefined
  onChange: (id: string, value: DraftValue) => void
}

function SignalValueCell({ signal, draft, onChange }: CellProps) {
  if (!signal.writable) {
    return <span className="mono">{formatValue(signal.value)}</span>
  }

  if (isBool(signal)) {
    const checked = draft !== undefined ? Boolean(draft) : Boolean(signal.value)
    return (
      <label className="signals-bool" title={signal.logic || ''}>
        <input type="checkbox" checked={checked} onChange={(e) => onChange(signal.id, e.target.checked)} />
        <span className="mono">{checked ? 'true' : 'false'}</span>
      </label>
    )
  }

  const isNum = isNumeric(signal)
  const value = draft !== undefined ? String(draft) : formatDraft(signal.value)

  return (
    <div className="signals-editor">
      <input
        type={isNum ? 'number' : 'text'}
        step={signal.type === 'float' ? 'any' : 1}
        value={value}
        onChange={(e) => onChange(signal.id, e.target.value)}
      />
      <span className="mono signals-current">тек. {formatValue(signal.value)}</span>
    </div>
  )
}

function formatDraft(value: Signal['value']): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

function draftToValue(s: Signal, draft: DraftValue): boolean | number | string {
  if (isBool(s)) return Boolean(draft)
  if (s.type === 'float') return Number(draft)
  if (isNumeric(s)) return Math.trunc(Number(draft))
  return String(draft)
}

function formatValue(value: Signal['value']): string {
  if (value === null || value === '') return '—'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}