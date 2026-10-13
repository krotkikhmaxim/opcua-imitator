import { useCallback, useEffect, useMemo, useState } from 'react'
import { stationApi } from '../../api'
import {
  StationSnapshotInfo,
  TelemetryBindingRow,
  TelemetryBindingState,
} from '../../types'
import './StationEchoPanel.css'

const REFRESH_MS = 2000

const STATES: TelemetryBindingState[] = [
  'active',
  'inactive',
  'connection_missing',
  'error',
  'bad_quality',
  'unbound',
]

const STATE_LABEL: Record<TelemetryBindingState, string> = {
  active: 'активен',
  inactive: 'неактивен',
  connection_missing: 'нет связи',
  error: 'ошибка',
  bad_quality: 'плохое качество',
  unbound: 'не привязан',
}

function isAxiosDetail(e: unknown): string | null {
  const detail = (e as { response?: { data?: { detail?: { code?: string; message?: string } } } })
    ?.response?.data?.detail
  if (!detail) return null
  if (typeof detail === 'string') return detail
  return detail.message || detail.code || null
}

function formatWhen(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function formatValue(v: TelemetryBindingRow['last_value']): string {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  return String(v)
}

/**
 * Эхо станции: таблица сигналов с её живыми состояниями через read-only
 * прокси. Панель только смотрит — снимок live_batch тоже читающая операция
 * (станция фиксирует то, что уже пришло по OPC UA).
 */
export default function StationEchoPanel() {
  const [bindings, setBindings] = useState<TelemetryBindingRow[]>([])
  const [down, setDown] = useState<string | null>(null)
  const [filter, setFilter] = useState<'all' | TelemetryBindingState>('all')
  const [snapshot, setSnapshot] = useState<StationSnapshotInfo | null>(null)
  const [snapshotBusy, setSnapshotBusy] = useState(false)
  const [snapshotErr, setSnapshotErr] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const res = await stationApi.bindings()
      setBindings(res.bindings)
      setDown(null)
    } catch (e) {
      setDown(isAxiosDetail(e) || 'Станция недоступна')
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, REFRESH_MS)
    return () => clearInterval(interval)
  }, [refresh])

  const counts = useMemo(() => {
    const map = new Map<TelemetryBindingState, number>()
    for (const b of bindings) map.set(b.state, (map.get(b.state) || 0) + 1)
    return map
  }, [bindings])

  const visible = useMemo(
    () =>
      filter === 'all' ? bindings : bindings.filter((b) => b.state === filter),
    [bindings, filter],
  )

  const takeSnapshot = useCallback(async () => {
    setSnapshotBusy(true)
    setSnapshotErr(null)
    try {
      setSnapshot(await stationApi.snapshot('live_batch'))
    } catch (e) {
      setSnapshotErr(isAxiosDetail(e) || 'Снимок не удался')
    } finally {
      setSnapshotBusy(false)
    }
  }, [])

  return (
    <div className="se">
      <div className="se-head">
        <h3>Станция — эхо сигналов</h3>
        <span className="se-sub">
          то, что станция видит по OPC UA (read-only прокси, обновление 2 с)
        </span>
        <span className={`se-link${down ? ' se-link-down' : ''}`}>
          {down ? 'нет связи' : 'связь есть'}
        </span>
        <button
          type="button"
          className="se-snapshot"
          disabled={snapshotBusy || !!down}
          onClick={takeSnapshot}
          title="POST /api/station/snapshots (mode=live_batch): станция фиксирует текущие значения"
        >
          {snapshotBusy ? 'Снимок…' : 'Снять снапшот'}
        </button>
      </div>

      {down && <div className="se-err">{down}</div>}

      {snapshot && (
        <div className="se-snapshot-info">
          Снапшот <span className="se-mono">{String(snapshot.id)}</span>
          {snapshot.status ? ` · статус ${String(snapshot.status)}` : ''}
        </div>
      )}
      {snapshotErr && <div className="se-err">{snapshotErr}</div>}

      <div className="se-filters">
        <button
          type="button"
          className={`se-chip${filter === 'all' ? ' se-chip-on' : ''}`}
          onClick={() => setFilter('all')}
        >
          все · {bindings.length}
        </button>
        {STATES.filter((s) => counts.get(s)).map((s) => (
          <button
            key={s}
            type="button"
            className={`se-chip se-chip-${s}${filter === s ? ' se-chip-on' : ''}`}
            onClick={() => setFilter(s)}
          >
            {STATE_LABEL[s]} · {counts.get(s)}
          </button>
        ))}
      </div>

      <div className="se-table-wrap">
        <table className="se-table">
          <thead>
            <tr>
              <th>Состояние</th>
              <th>Сигнал</th>
              <th>Шкаф</th>
              <th>Значение</th>
              <th>Метка источника</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((b) => (
              <tr key={b.id}>
                <td>
                  <span className={`se-badge se-badge-${b.state}`}>{STATE_LABEL[b.state] || b.state}</span>
                </td>
                <td>
                  <div className="se-name">{b.name}</div>
                  <div className="se-mono se-id" title={`${b.project_tag_id} · ${b.id}`}>
                    {b.id}
                  </div>
                </td>
                <td className="se-mono">{b.cabinet || '—'}</td>
                <td className="se-mono">
                  {formatValue(b.last_value)}
                  {b.last_quality && b.last_quality !== 'good' ? (
                    <span className="se-quality"> · {b.last_quality}</span>
                  ) : null}
                </td>
                <td className="se-mono">{formatWhen(b.last_source_timestamp)}</td>
              </tr>
            ))}
            {visible.length === 0 && !down && (
              <tr>
                <td colSpan={5} className="se-none">
                  {bindings.length === 0 ? 'Нет данных — станция не отдала биндинги' : 'В этом состоянии сигналов нет'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
