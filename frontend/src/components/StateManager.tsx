import { useCallback, useEffect, useRef, useState } from 'react'
import { statesApi } from '../api'
import { AppMessage, ImportResponse, StateInfo } from '../types'
import './StateManager.css'

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)}MB`
}

function formatDate(iso: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const AUTO_REFRESH_MS = 10000

interface StateManagerProps {
  onStateLoaded?: () => void
}

export default function StateManager({ onStateLoaded }: StateManagerProps) {
  const [states, setStates] = useState<StateInfo[]>([])
  const [message, setMessage] = useState<AppMessage | null>(null)
  const [loading, setLoading] = useState(false)
  const [actionId, setActionId] = useState<string | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [saveName, setSaveName] = useState('')
  const [saveDesc, setSaveDesc] = useState('')
  const [signalCount, setSignalCount] = useState<number | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const showMsg = useCallback((kind: AppMessage['kind'], text: string) => {
    setMessage({ kind, text })
    window.setTimeout(() => setMessage(null), 5000)
  }, [])

  const loadStates = useCallback(async () => {
    try {
      const data = await statesApi.list()
      setStates(data)
    } catch (e: any) {
      showMsg('error', e?.response?.data?.detail || 'Ошибка загрузки списка состояний')
    }
  }, [showMsg])

  useEffect(() => {
    loadStates()
    const interval = setInterval(loadStates, AUTO_REFRESH_MS)
    return () => clearInterval(interval)
  }, [loadStates])

  const handleSave = async () => {
    if (!saveName.trim()) {
      showMsg('error', 'Введите название состояния')
      return
    }
    setLoading(true)
    setActionId('save')
    try {
      const res = await statesApi.save(saveName, saveDesc)
      showMsg('success', res.message)
      setModalOpen(false)
      setSaveName('')
      setSaveDesc('')
      setSignalCount(null)
      await loadStates()
    } catch (e: any) {
      showMsg('error', e?.response?.data?.detail || 'Ошибка сохранения состояния')
    } finally {
      setLoading(false)
      setActionId(null)
    }
  }

  const handleLoad = async (filename: string) => {
    if (!window.confirm('Загрузить выбранное состояние в OPC UA сервер?')) return
    setLoading(true)
    setActionId(filename)
    try {
      const res = await statesApi.load(filename)
      const completed = res.errors && res.errors.length > 0
      showMsg(
        completed ? 'info' : 'success',
        res.message || `Загружено сигналов: ${res.loaded_count}`,
      )
      onStateLoaded && onStateLoaded()
    } catch (e: any) {
      showMsg('error', e?.response?.data?.detail || 'Ошибка загрузки состояния')
    } finally {
      setLoading(false)
      setActionId(null)
    }
  }

  const handleDelete = async (filename: string) => {
    if (!window.confirm('Удалить выбранное состояние?')) return
    setLoading(true)
    setActionId(`del-${filename}`)
    try {
      await statesApi.remove(filename)
      showMsg('success', 'Состояние удалено')
      await loadStates()
    } catch (e: any) {
      showMsg('error', e?.response?.data?.detail || 'Ошибка удаления состояния')
    } finally {
      setLoading(false)
      setActionId(null)
    }
  }

  const handleExport = async (name: string, description: string) => {
    setLoading(true)
    setActionId(`exp-${name}`)
    try {
      const blob = await statesApi.export(name || 'state', description || '')
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${name || 'state'}.json`
      a.click()
      URL.revokeObjectURL(url)
      showMsg('success', 'Состояние экспортировано')
    } catch (e: any) {
      showMsg('error', 'Ошибка экспорта состояния')
    } finally {
      setLoading(false)
      setActionId(null)
    }
  }

  const handleImport = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setLoading(true)
    setActionId('import')
    try {
      const res: ImportResponse = await statesApi.import(file)
      if (res.errors && res.errors.length > 0) {
        showMsg('info', `Импортировано с предупреждениями: ${res.errors.join('; ')}`)
      } else {
        showMsg('success', `Состояние импортировано (${res.signal_count} сигналов)`)
      }
      await loadStates()
    } catch (e: any) {
      showMsg('error', e?.response?.data?.detail || 'Неверный JSON формат')
    } finally {
      setLoading(false)
      setActionId(null)
    }
  }

  const openModal = async () => {
    setModalOpen(true)
    try {
      const s = await fetch('/api/signals').then((r) => r.json())
      const count = s.signals
        ? Object.values(s.signals).filter((sig: any) => sig.value !== null && sig.value !== '').length
        : null
      setSignalCount(count)
    } catch {
      setSignalCount(null)
    }
  }

  const isBusy = (id: string | null) => loading && actionId === id

  return (
    <section className="sm">
      <div className="sm-toolbar">
        <button className="sm-btn sm-btn-primary" disabled={loading} onClick={openModal}>
          💾 Сохранить состояние
        </button>
        <button
          className="sm-btn"
          disabled={loading}
          onClick={() => fileInputRef.current?.click()}
        >
          📥 Импорт
        </button>
        <button className="sm-btn" disabled={loading} onClick={loadStates}>
          🔄 Обновить
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".json,application/json"
          style={{ display: 'none' }}
          onChange={handleImport}
        />
      </div>

      {message && <div className={`sm-message sm-${message.kind}`}>{message.text}</div>}

      <h3 className="sm-title">Сохранённые состояния ({states.length})</h3>

      {states.length === 0 && !loading ? (
        <div className="sm-empty">Нет сохранённых состояний</div>
      ) : (
        <div className="sm-grid">
          {states.map((s) => (
            <div className="sm-card" key={s.filename}>
              <div className="sm-card-head">
                <span className="sm-card-name">{s.name || s.filename}</span>
                {s.signal_count !== undefined && (
                  <span className="sm-badge">{s.signal_count} сигн.</span>
                )}
              </div>
              {s.description && <p className="sm-card-desc">{s.description}</p>}
              <div className="sm-card-meta">
                <span>📅 {formatDate(s.created)}</span>
                <span>📄 {formatSize(s.size)}</span>
              </div>
              <div className="sm-card-actions">
                <button
                  className="sm-btn sm-btn-small"
                  disabled={loading}
                  onClick={() => handleLoad(s.filename)}
                >
                  {isBusy(s.filename) ? 'Загрузка...' : '▶ Загрузить'}
                </button>
                <button
                  className="sm-btn sm-btn-small"
                  disabled={loading}
                  onClick={() => handleExport(s.name || s.filename, s.description || '')}
                >
                  📤 Экспорт
                </button>
                <button
                  className="sm-btn sm-btn-small sm-btn-danger"
                  disabled={loading}
                  onClick={() => handleDelete(s.filename)}
                >
                  🗑 Удалить
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {modalOpen && (
        <div className="sm-overlay" onClick={() => !loading && setModalOpen(false)}>
          <div className="sm-modal" onClick={(e) => e.stopPropagation()}>
            <h3 className="sm-modal-title">💾 Сохранить состояние</h3>
            <label className="sm-field">
              <span>Название состояния *</span>
              <input
                value={saveName}
                onChange={(e) => setSaveName(e.target.value)}
                placeholder="Режим работы №1"
                autoFocus
              />
            </label>
            <label className="sm-field">
              <span>Описание</span>
              <textarea
                value={saveDesc}
                onChange={(e) => setSaveDesc(e.target.value)}
                placeholder="Настройки для нормальной работы"
                rows={3}
              />
            </label>
            {signalCount !== null && (
              <p className="sm-signalinfo">Будет сохранено {signalCount} сигналов</p>
            )}
            <div className="sm-modal-actions">
              <button
                className="sm-btn"
                disabled={loading || !saveName.trim()}
                onClick={() => setModalOpen(false)}
              >
                Отмена
              </button>
              <button
                className="sm-btn sm-btn-primary"
                disabled={loading || !saveName.trim()}
                onClick={handleSave}
              >
                {isBusy('save') ? 'Сохранение...' : '💾 Сохранить'}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
