import { useCallback, useEffect, useState } from 'react'
import { scenariosApi } from '../../api'
import { ScenarioStatus } from '../../types'
import './EmergencyButtons.css'

const STATUS_POLL_MS = 2000

interface Props {
  onNotify: (kind: 'ok' | 'err', text: string) => void
}

/**
 * Пульт аварий: по кнопке на синтетический сценарий (one-shot) и «Стоп».
 * Статус опрашивается сам — активная кнопка подсвечивается, пока движок
 * проигрывает её эпизод.
 */
export default function EmergencyButtons({ onNotify }: Props) {
  const [kinds, setKinds] = useState<{ kind: string; title: string; fault: boolean }[]>([])
  const [status, setStatus] = useState<ScenarioStatus | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const s = await scenariosApi.status()
      setStatus(s)
    } catch {
      /* backend недоступен — оставляем прошлое */
    }
  }, [])

  useEffect(() => {
    scenariosApi
      .describe()
      .then((d) => setKinds(d.kinds))
      .catch(() => setKinds([]))
    refresh()
    const interval = setInterval(refresh, STATUS_POLL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  const run = useCallback(
    async (action: () => Promise<ScenarioStatus>, failText: string) => {
      setBusy(true)
      try {
        setStatus(await action())
      } catch (e: any) {
        onNotify('err', e?.response?.data?.detail || failText)
        refresh()
      } finally {
        setBusy(false)
      }
    },
    [onNotify, refresh],
  )

  const running = status?.running ?? false
  const runningKind = running && status?.kind ? status.kind : null

  return (
    <div className="eb">
      <div className="eb-label">Пульт аварий:</div>
      <div className="eb-buttons">
        {kinds.map((k) => {
          const active = runningKind === k.kind
          return (
            <button
              key={k.kind}
              type="button"
              className={`eb-btn ${k.fault ? 'eb-btn-fault' : 'eb-btn-normal'}${active ? ' eb-btn-active' : ''}`}
              disabled={busy || (running && !active)}
              title={k.fault ? `Запустить эпизод «${k.title}»` : `Контрольный эпизод «${k.title}»`}
              onClick={() =>
                run(
                  () => scenariosApi.startKinds([k.kind], true),
                  `Ошибка запуска «${k.title}»`,
                )
              }
            >
              {active ? '● ' : ''}
              {k.title}
            </button>
          )
        })}
        {running && (
          <button
            type="button"
            className="eb-btn eb-btn-stop"
            disabled={busy}
            onClick={() => run(() => scenariosApi.stop(), 'Ошибка остановки')}
          >
            Стоп
          </button>
        )}
        {kinds.length === 0 && <span className="eb-empty">Сценарии недоступны</span>}
      </div>
    </div>
  )
}
