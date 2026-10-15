import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import RecordedModesPanel from './components/RecordedModesPanel'
import StateManager from './components/StateManager'
import EmergencyButtons from './components/mnemo/EmergencyButtons'
import MnemoPanel from './components/mnemo/MnemoPanel'
import TruthJournalPanel from './components/mnemo/TruthJournalPanel'
import { useSignalStream, type OwnerEntry, type SignalEntry } from './components/mnemo/useSignalStream'
import ArchivePlaybackPanel from './components/playback/ArchivePlaybackPanel'
import PlaybackPanel from './components/playback/PlaybackPanel'
import TimelineBadge from './components/playback/TimelineBadge'
import TimelinePanel from './components/playback/TimelinePanel'
import TimelineStrip from './components/playback/TimelineStrip'
import SchematicView from './components/schematic/SchematicView'
import StationEchoPanel from './components/station/StationEchoPanel'
import { heartbeatApi, schematicApi, signalsApi } from './api'
import { HeartbeatResponse, SchematicBridge, Signal, WriteBatchResponse } from './types'
import './App.css'

interface SignalsResponse {
  signals: Record<string, Omit<Signal, 'id'> & { id?: string }>
}

type Tab = 'emergency' | 'modes' | 'table'

type SimMode = 'schematic' | 'mnemo'

// Два вида внутри «Сценариев и режимов»: таймлайн (прогон тренда по
// реальному времени) и стат-режимы (синтетические эпизоды и записанные
// режимы RecordedModesPanel, проигрываются в темпе).
type ModesView = 'timeline' | 'modes'

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

/** Сырая карточка сервера → Signal; общий для REST-опроса и WS-снимка. */
function toSignal(id: string, s: Record<string, unknown>): Signal {
  return {
    id,
    short_id: s.short_id as string | undefined,
    name: s.name as string,
    type: s.type as string,
    writable: Boolean(s.writable),
    value: s.value as Signal['value'],
    revision: s.revision as number | undefined,
    source: (s.source as string | null) ?? null,
    changed_at: (s.changed_at as string | null) ?? null,
    owner: (s.owner as string | null) ?? null,
    owner_id: (s.owner_id as string | null) ?? null,
    direction: s.direction as string | undefined,
    channel: s.channel as string | undefined,
    module: s.module as string | undefined,
    cabinet: s.cabinet as string | undefined,
    device: s.device as string | undefined,
    address: s.address as string | undefined,
    project_tag: s.project_tag as string | undefined,
    bit: s.bit as string | number | undefined,
    word_address: s.word_address as string | number | undefined,
    logic: s.logic as string | undefined,
  }
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
  const [tab, setTab] = useState<Tab>('modes')
  // Режим внутри «Имитации аварий»: лист Э3 или панель мнемо.
  const [simMode, setSimMode] = useState<SimMode>('schematic')
  // Вид внутри «Сценариев и режимов»; при загрузке страницы встречают
  // сценарии и режимы.
  const [modesView, setModesView] = useState<ModesView>('modes')
  // Мост «обозначение Э3 → сигналы»: карта сигнал→аппарат и выбранный аппарат
  // (клик по бейджу схемы подсвечивает его сигналы в мнемо и наоборот).
  const [bridge, setBridge] = useState<SchematicBridge | null>(null)
  const [bridgeError, setBridgeError] = useState<string | null>(null)
  const [selectedDevice, setSelectedDevice] = useState<string | null>(null)
  const statusTimer = useRef<number | null>(null)

  const refreshSignals = useCallback(async () => {
    try {
      const res = await fetch('/api/signals').then((r): Promise<SignalsResponse> => r.json())
      setSignals(Object.entries(res.signals).map(([id, s]) => toSignal(id, s)))
      setUpdatedAt(new Date().toLocaleTimeString())
    } catch {
      // ignore
    }
  }, [])

  // §12: дельта применяется, только если она не старше известной ревизии —
  // устаревшее событие не откатывает подтверждённое состояние.
  const applyDeltas = useCallback(
    (changes: Record<string, SignalEntry>, owners?: Record<string, OwnerEntry>) => {
      setSignals((prev) =>
        prev.map((s) => {
          let next = s
          const ownerEntry = owners ? owners[s.id] : undefined
          if (ownerEntry !== undefined) {
            next = {
              ...next,
              owner: ownerEntry ? ownerEntry.title : null,
              owner_id: ownerEntry ? ownerEntry.id : null,
            }
          }
          const entry = changes[s.id]
          if (!entry) return next
          const rev = typeof entry.r === 'number' ? entry.r : 0
          if (typeof next.revision === 'number' && rev < next.revision) return next
          return {
            ...next,
            value: entry.v as Signal['value'],
            revision: rev,
            source: entry.s !== undefined ? entry.s : next.source,
          }
        }),
      )
    },
    [],
  )

  const { connected: streamConnected, activeDrivers } = useSignalStream({
    onSignals: applyDeltas,
    onSnapshot: useCallback((raw: Record<string, Record<string, unknown>>) => {
      setSignals(Object.entries(raw).map(([id, s]) => toSignal(id, s)))
    }, []),
    onGap: useCallback(() => {
      void refreshSignals()
    }, [refreshSignals]),
  })
  const driverActive = activeDrivers.length > 0

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

  // Мост читается один раз; его нет (не собран) — схема не показывается,
  // а остальной UI живёт как прежде.
  useEffect(() => {
    schematicApi
      .bridge()
      .then((b) => {
        setBridge(b)
        setBridgeError(null)
      })
      .catch((e) => {
        const code = e?.response?.data?.detail?.code
        setBridgeError(
          code === 'bridge_not_built'
            ? 'Мост схема↔сигналы не собран (scripts/build_schematic_bridge.py)'
            : 'Мост схема↔сигналы недоступен',
        )
      })
  }, [])

  const deviceBySignal = useMemo(() => {
    const map: Record<string, string> = {}
    if (bridge) {
      for (const [dt, dev] of Object.entries(bridge.devices)) {
        for (const id of dev.signals) map[id] = dt
      }
    }
    return map
  }, [bridge])

  // Клик по имени сигнала («Аппарат U1 — показать на схеме Э3»): выбрать
  // аппарат и показать его лист. Обратный переход не автоматический — бейдж
  // на схеме просто подсвечивается, режим мнемо оператор выбирает сам.
  const selectFromMnemo = useCallback((device: string | null) => {
    setSelectedDevice(device)
    if (device !== null) setSimMode('schematic')
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

  // §8: в представления попадает только подтверждённый сервером результат
  // команды; оптимистичной подмены значения нет.
  const applyConfirmed = useCallback(
    (res: WriteBatchResponse, source: string) => {
      if (!res.results) return
      const confirmed: Record<string, SignalEntry> = {}
      for (const [id, entry] of Object.entries(res.results)) {
        if (entry.status === 'applied') {
          confirmed[id] = { v: entry.value, r: entry.revision ?? 0, s: source }
        }
      }
      if (Object.keys(confirmed).length > 0) applyDeltas(confirmed)
    },
    [applyDeltas],
  )

  const applyChanges = useCallback(async () => {
    const values: Record<string, boolean | number | string> = {}
    const expected_revisions: Record<string, number> = {}
    for (const id of Object.keys(drafts)) {
      const cfg = signalsById.current[id]
      if (!cfg || !cfg.writable) continue
      values[id] = draftToValue(cfg, drafts[id])
      // §8: ревизия на момент чтения клиентом; если она ушла — сервер
      // отклонит пакет, и мы запросим актуальное состояние, а не перезапишем
      // чужое изменение незаметно.
      if (typeof cfg.revision === 'number') expected_revisions[id] = cfg.revision
    }
    const ids = Object.keys(values)
    if (ids.length === 0) {
      showStatus('err', 'Нет изменений для применения')
      return
    }
    setApplying(true)
    try {
      const res = await signalsApi.writeBatch(values, {
        op_id: crypto.randomUUID(),
        expected_revisions:
          Object.keys(expected_revisions).length > 0 ? expected_revisions : undefined,
      })
      applyConfirmed(res, 'manual_batch')
      if (res.errors.length > 0) {
        showStatus('err', `Применено ${res.applied.length} из ${ids.length}. Ошибки: ${res.errors.join('; ')}`)
      } else {
        showStatus('ok', `Применено значений: ${res.applied.length}`)
      }
      resetDrafts(res.applied)
      refreshSignals()
    } catch (e: any) {
      // §8: при конфликте (устаревший снимок, владение) — запросить
      // актуальное состояние сервера, а не угадывать его локально.
      showStatus('err', e?.response?.data?.detail || 'Ошибка применения значений')
      refreshSignals()
    } finally {
      setApplying(false)
    }
  }, [drafts, applyConfirmed, refreshSignals, resetDrafts, showStatus])

  // Сброс выбранных (черновых) сигналов к начальным значениям реестра — через
  // общий механизм применения; таймлайн не затрагивается (ТЗ #5 §8).
  const resetSelected = useCallback(async () => {
    const ids = Object.keys(drafts).filter((id) => signalsById.current[id]?.writable)
    if (ids.length === 0) return
    setApplying(true)
    try {
      const res = await signalsApi.reset(ids)
      applyConfirmed(res, 'reset')
      if (res.errors.length > 0) {
        showStatus('err', `Сброшено ${res.applied.length} из ${ids.length}. Ошибки: ${res.errors.join('; ')}`)
      } else {
        showStatus('ok', `Сброшено к начальным: ${res.applied.length}`)
      }
      resetDrafts(res.applied)
      refreshSignals()
    } catch (e: any) {
      showStatus('err', e?.response?.data?.detail || 'Ошибка сброса значений')
      refreshSignals()
    } finally {
      setApplying(false)
    }
  }, [drafts, applyConfirmed, refreshSignals, resetDrafts, showStatus])

  const resetAll = useCallback(async () => {
    if (
      !window.confirm(
        'Сбросить ВСЕ записываемые сигналы к начальным значениям реестра?',
      )
    )
      return
    setApplying(true)
    try {
      const res = await signalsApi.reset()
      applyConfirmed(res, 'reset')
      if (res.errors.length > 0) {
        showStatus('err', `Сброшено ${res.applied.length}. Ошибки: ${res.errors.join('; ')}`)
      } else {
        showStatus('ok', `Сброшено к начальным: ${res.applied.length}`)
      }
      resetDrafts(Object.keys(drafts))
      refreshSignals()
    } catch (e: any) {
      showStatus('err', e?.response?.data?.detail || 'Ошибка сброса значений')
      refreshSignals()
    } finally {
      setApplying(false)
    }
  }, [drafts, applyConfirmed, refreshSignals, resetDrafts, showStatus])

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
          <div className="app-header-badges">
            <span
              className={`app-conn app-conn-${streamConnected ? 'live' : 'poll'}`}
              title={streamConnected ? 'Живые значения по WebSocket' : 'WebSocket недоступен — значения по опросу'}
            >
              {streamConnected ? 'live' : 'опрос'}
            </span>
            {driverActive && (
              <span className="app-driver" title={`Активный драйвер: ${activeDrivers.join(', ')}`}>
                драйвер: {activeDrivers.join(', ')}
              </span>
            )}
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
        </div>
      </header>

      {/* §12: связь потеряна — значения помечаются устаревшими, управление
          блокируется до восстановления (кнопки ниже и мнемо отключены). */}
      {!streamConnected && (
        <div className="signals-status signals-status-err app-offline">
          Связь с сервером потеряна — значения устарели, управление заблокировано
        </div>
      )}

      {/* Полоса таймлайна: живёт под шапкой на всех вкладках — показывает
          идущий прогон и сообщает об останове при применении сценария. */}
      <TimelineStrip />

      <div className="app-tabs">
        <button
          type="button"
          className={`app-tab${tab === 'modes' ? ' app-tab-on' : ''}`}
          onClick={() => setTab('modes')}
        >
          Сценарии и режимы
        </button>
        <button
          type="button"
          className={`app-tab${tab === 'table' ? ' app-tab-on' : ''}`}
          onClick={() => setTab('table')}
        >
          Таблица сигналов
        </button>
        <button
          type="button"
          className={`app-tab${tab === 'emergency' ? ' app-tab-on' : ''}`}
          onClick={() => setTab('emergency')}
        >
          Имитация аварий
        </button>
      </div>

      {/* Пульт состояний нужен там, где значениями управляют руками:
          имитация аварий и таблица сигналов; на сценариях он не нужен. */}
      {(tab === 'emergency' || tab === 'table') && (
        <StateManager onStateLoaded={handleStateLoaded} />
      )}

      {tab === 'modes' && (
        <>
          <div className="sim-switch">
            <button
              type="button"
              className={modesView === 'modes' ? 'sim-switch-on' : ''}
              onClick={() => setModesView('modes')}
            >
              Статические режимы
            </button>
            <button
              type="button"
              className={modesView === 'timeline' ? 'sim-switch-on' : ''}
              onClick={() => setModesView('timeline')}
            >
              Таймлайн
            </button>
          </div>
          {modesView === 'timeline' && <TimelinePanel onNotify={showStatus} />}
          {modesView === 'modes' && (
            <>
              <TimelineBadge onOpen={() => setModesView('timeline')} />
              <RecordedModesPanel onModeChanged={refreshSignals} />
            </>
          )}
        </>
      )}

      {tab === 'emergency' && (
        <>
          <div className="sim-switch">
            <button
              type="button"
              className={simMode === 'schematic' ? 'sim-switch-on' : ''}
              onClick={() => setSimMode('schematic')}
            >
              По схеме Э3
            </button>
            <button
              type="button"
              className={simMode === 'mnemo' ? 'sim-switch-on' : ''}
              onClick={() => setSimMode('mnemo')}
            >
              По мнемосхеме
            </button>
          </div>
          {simMode === 'schematic' && (
            <>
              {bridgeError && (
                <div className="signals-status signals-status-err">{bridgeError}</div>
              )}
              {bridge && (
                <SchematicView
                  signals={signals}
                  bridge={bridge}
                  selectedDevice={selectedDevice}
                  onSelectDevice={setSelectedDevice}
                />
              )}
            </>
          )}
          {simMode === 'mnemo' && (
            <div className="mnemo-tab">
              <TimelineBadge
                onOpen={() => {
                  setTab('modes')
                  setModesView('timeline')
                }}
              />
              <EmergencyButtons onNotify={showStatus} />
              <PlaybackPanel onNotify={showStatus} />
              <ArchivePlaybackPanel onNotify={showStatus} />
              {status && (
                <div className={`signals-status signals-status-${status.kind}`}>{status.text}</div>
              )}
              <MnemoPanel
                signals={signals}
                driverActive={driverActive}
                showStatus={showStatus}
                deviceBySignal={deviceBySignal}
                selectedDevice={selectedDevice}
                onSelectDevice={selectFromMnemo}
                onWritten={refreshSignals}
                offline={!streamConnected}
              />
              <TruthJournalPanel />
              <StationEchoPanel />
            </div>
          )}
        </>
      )}

      {tab === 'table' && (
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
            disabled={applying || !streamConnected || changedCount === 0}
            title={streamConnected ? undefined : 'Связь с сервером потеряна'}
            onClick={applyChanges}
          >
            {applying ? 'Применение…' : `Применить${changedCount > 0 ? ` (${changedCount})` : ''}`}
          </button>
          {changedCount > 0 && (
            <button className="signals-cancel" disabled={applying || !streamConnected} onClick={() => resetDrafts(Object.keys(drafts))}>
              Сбросить
            </button>
          )}
          {changedCount > 0 && (
            <button
              className="signals-cancel"
              disabled={applying || !streamConnected}
              title="Сбросить выбранные сигналы к начальным значениям реестра"
              onClick={resetSelected}
            >
              ⟲ К начальным ({changedCount})
            </button>
          )}
          <button
            className="signals-cancel"
            disabled={applying || !streamConnected}
            title="Сбросить весь записываемый набор к начальным значениям реестра"
            onClick={resetAll}
          >
            ⟲ Всё к начальным
          </button>
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
                    <th>Статус</th>
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
                      <td className="signals-statuscell">
                        {/* §12: источник последнего изменения и владелец. */}
                        <div className="signals-tag mono" title={s.source ? `Источник: ${s.source}` : 'Источник: —'}>
                          {s.source || '—'}
                        </div>
                        {s.owner && (
                          <div className="signals-owner" title={`Держит: ${s.owner_id}`}>
                            🔒 {s.owner}
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                  {list.length === 0 && (
                    <tr>
                      <td colSpan={8} className="signals-empty">Нет сигналов</td>
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
      )}
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