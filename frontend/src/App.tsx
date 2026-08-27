import { useCallback, useEffect, useState } from 'react'
import StateManager from './components/StateManager'
import { Signal } from './types'
import './App.css'

interface SignalsResponse {
  signals: Record<string, Omit<Signal, 'id'> & { id?: string }>
}

export default function App() {
  const [signals, setSignals] = useState<Signal[]>([])
  const [updatedAt, setUpdatedAt] = useState<string>('')

  const refreshSignals = useCallback(async () => {
    try {
      const res = await fetch('/api/signals').then((r): Promise<SignalsResponse> => r.json())
      const list: Signal[] = Object.entries(res.signals).map(([id, s]) => ({
        id,
        name: s.name,
        type: s.type,
        writable: s.writable,
        value: s.value,
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

  const formatValue = (s: Signal): string => {
    if (s.value === null || s.value === '') return '—'
    if (typeof s.value === 'boolean') return s.value ? 'true' : 'false'
    return String(s.value)
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>ОПК UA — Мониторинг подъёмной машины</h1>
        <p className="app-subtitle">Сохранение и загрузка состояния сигналов</p>
      </header>

      <StateManager onStateLoaded={handleStateLoaded} />

      <section className="signals-panel">
        <div className="signals-head">
          <h2>Текущие сигналы</h2>
          {updatedAt && <span className="signals-updated">обновлено {updatedAt}</span>}
        </div>
        <div className="signals-table-wrap">
          <table className="signals-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Название</th>
                <th>Тип</th>
                <th>Доступ</th>
                <th>Значение</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.id}>
                  <td className="mono">{s.id}</td>
                  <td>{s.name}</td>
                  <td>{s.type}</td>
                  <td>{s.writable ? 'запись' : 'только чтение'}</td>
                  <td className="mono">{formatValue(s)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
