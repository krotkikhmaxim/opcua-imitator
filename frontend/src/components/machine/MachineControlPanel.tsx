import { useCallback, useEffect, useRef, useState } from 'react'
import { machineApi } from '../../api'
import { MachineStatus } from '../../types'
import './MachineControlPanel.css'

const POLL_MS = 1500

interface Props {
  onShowSignals: () => void
}

type Busy = null | 'start' | 'event' | 'remedy' | 'finish' | 'restart'

function errorText(e: any, fallback: string): string {
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d: any) => d?.msg).filter(Boolean).join('; ')
  return fallback
}

function clock(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export default function MachineControlPanel({ onShowSignals }: Props) {
  const [status, setStatus] = useState<MachineStatus | null>(null)
  const [selected, setSelected] = useState<string>('moving')
  const [busy, setBusy] = useState<Busy>(null)
  const [error, setError] = useState<string | null>(null)
  const selectedTouched = useRef(false)

  const apply = useCallback((next: MachineStatus) => {
    setStatus(next)
    // Выбор карточки оператора не перебиваем, пока он сам не переключит.
    if (!selectedTouched.current && next.state) setSelected(next.state)
  }, [])

  const refresh = useCallback(async () => {
    try {
      apply(await machineApi.status())
    } catch {
      /* опрос не должен ломать экран — следующая попытка через интервал */
    }
  }, [apply])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, POLL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  const run = useCallback(
    async (kind: Busy, action: () => Promise<MachineStatus>, failText: string) => {
      setBusy(kind)
      setError(null)
      try {
        apply(await action())
      } catch (e: any) {
        setError(errorText(e, failText))
        refresh()
      } finally {
        setBusy(null)
      }
    },
    [apply, refresh],
  )

  const phase = status?.phase ?? 'idle'
  const running = phase === 'running' || phase === 'event'
  const locked = busy !== null || (status?.busy ?? false)
  const active = status?.active_event ?? null
  const notice = status?.notice ?? null
  const observed = status?.observed
  const events = status?.events ?? []
  const normalEvents = events.filter((e) => e.group === 'normal')
  const faultEvents = events.filter((e) => e.group === 'fault')
  const selectedCard = status?.states.find((s) => s.kind === selected) ?? null
  const selectionPending = running && !!status?.state && status.state !== selected

  const stateCards = status?.states ?? []

  // Порядок блоков: «Что происходит сейчас?» сверху — это сводка по фактическим
  // сигналам, ниже выбор состояния и события. Владелец смотрит на неё первой.
  return (
    <section className="mc">
      <div className="mc-step mc-step-now">
        <div className="mc-step-head">
          <span className="mc-step-num">1</span>
          <h2>Что происходит сейчас?</h2>
        </div>

        <div className={`mc-status mc-status-${active ? 'fault' : running ? 'run' : 'idle'}`}>
          <div className="mc-status-main">
            <div className="mc-status-headline">{status?.headline ?? 'Машина отключена'}</div>
            <div className="mc-status-detail">{status?.detail ?? 'Пульт ещё не получал сигналы'}</div>
            <div className="mc-status-impact">
              {active ? `Активное воздействие: ${active.title}` : 'Активных воздействий нет'}
            </div>
          </div>

          {observed && (
            <dl className="mc-observed">
              <div>
                <dt>Скорость</dt>
                <dd className="mono">{observed.speed_text}</dd>
              </div>
              <div>
                <dt>Реле ТП</dt>
                <dd className={`mono${observed.tp_relay ? '' : ' mc-bad'}`}>{observed.tp_relay_text}</dd>
              </div>
              <div>
                <dt>Цепь аварийного останова</dt>
                <dd className={`mono${observed.estop_circuit ? '' : ' mc-bad'}`}>
                  {observed.estop_circuit_text}
                </dd>
              </div>
              <div>
                <dt>Ошибка привода</dt>
                <dd className={`mono${observed.drive_fault ? ' mc-bad' : ''}`}>
                  {observed.drive_fault_text}
                </dd>
              </div>
            </dl>
          )}

          {status?.last_action && (
            <div className="mc-last">
              Последнее действие: {clock(status.last_action.at)} — {status.last_action.text}
            </div>
          )}

          <button type="button" className="mc-link" onClick={onShowSignals}>
            Показать все сигналы →
          </button>
        </div>

        <div className="mc-actions">
          <button
            type="button"
            className="mc-finish"
            disabled={!status?.can_finish || locked}
            onClick={() => run('finish', () => machineApi.finish(), 'Ошибка завершения теста')}
          >
            {busy === 'finish' ? 'Завершение…' : 'Завершить тест'}
          </button>
          <button
            type="button"
            className="mc-restart"
            disabled={!status?.can_restart || locked}
            onClick={() =>
              run('restart', () => machineApi.restart(selected), 'Ошибка нового запуска')
            }
          >
            {busy === 'restart' ? 'Перезапуск…' : 'Начать заново'}
          </button>
          {phase === 'finished' && (
            <span className="mc-finished">Тест завершён: значения оставлены для просмотра</span>
          )}
        </div>

        <details className="mc-fold">
          <summary>История событий</summary>
          {status && status.history.length > 0 ? (
            <ul className="mc-history">
              {status.history.map((entry, index) => (
                <li key={`${entry.at}-${index}`}>
                  <span className="mono mc-history-time">{clock(entry.at)}</span>
                  <span>{entry.text}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mc-hint">Событий пока нет.</p>
          )}
        </details>

        <details className="mc-fold">
          <summary>Дополнительные настройки</summary>
          <div className="mc-settings-row">
            <div>
              <div className="mc-settings-title">Heartbeat</div>
              <p className="mc-hint">
                Раз в секунду перезаписывает значения и обновляет SourceTimestamp, чтобы
                станция видела «свежие» данные. На время теста отключается сам: иначе
                метки времени воздействий перестают быть точными.
              </p>
              <p className="mc-hint">
                Включается и выключается кнопкой записи в правом верхнем углу страницы.
              </p>
            </div>
          </div>
        </details>
      </div>

      <div className="mc-step">
        <div className="mc-step-head">
          <span className="mc-step-num">2</span>
          <h2>В каком состоянии запустить машину?</h2>
        </div>

        <div className="mc-states">
          {stateCards.map((state) => {
            const on = selected === state.kind
            return (
              <button
                key={state.kind}
                type="button"
                className={`mc-state${on ? ' mc-state-on' : ''}`}
                onClick={() => {
                  selectedTouched.current = true
                  setSelected(state.kind)
                }}
              >
                <span className="mc-state-title">{state.title}</span>
                <span className="mc-state-hint">{state.hint}</span>
                {on && <span className="mc-state-check">✓ Выбрано</span>}
              </button>
            )
          })}
        </div>

        <button
          type="button"
          className="mc-start"
          disabled={!status?.can_start || locked}
          onClick={() => run('start', () => machineApi.start(selected), 'Ошибка запуска машины')}
        >
          {busy === 'start' ? 'Запуск…' : 'Запустить машину в выбранном состоянии'}
        </button>
        {status && !status.can_start && (
          <p className="mc-hint">
            Тест уже идёт. Новое состояние применится после «Начать заново» или «Завершить тест».
          </p>
        )}
        {selectionPending && (
          <p className="mc-hint mc-hint-warn">
            Выбрано «{selectedCard?.title ?? selected}»: применится после «Начать заново».
          </p>
        )}
      </div>

      <div className="mc-step">
        <div className="mc-step-head">
          <span className="mc-step-num">3</span>
          <h2>Что должно произойти?</h2>
        </div>
        <p className="mc-step-sub">Выберите событие после запуска машины.</p>

        {status?.events_hint && <p className="mc-note">{status.events_hint}</p>}

        {active && (
          <div className="mc-active">
            <div>
              <div className="mc-active-title">{active.title}</div>
              <div className="mc-active-since">с {clock(active.since)}</div>
            </div>
            <button
              type="button"
              className="mc-remedy"
              disabled={locked || phase === 'finished'}
              onClick={() => run('remedy', () => machineApi.remedy(), 'Ошибка устранения воздействия')}
            >
              {busy === 'remedy' ? 'Устранение…' : active.remedy || 'Устранить'}
            </button>
          </div>
        )}

        {notice && (
          <div className="mc-notice">
            <span>{notice.text}</span>
            {notice.repeat_event && (
              <button
                type="button"
                className="mc-repeat"
                disabled={locked || phase === 'finished'}
                onClick={() =>
                  run(
                    'event',
                    () => machineApi.event(notice.repeat_event as string),
                    'Ошибка повторного разрыва',
                  )
                }
              >
                Повторить разрыв
              </button>
            )}
          </div>
        )}

        {normalEvents.length > 0 && (
          <div className="mc-group">
            <div className="mc-group-title">Штатное действие</div>
            <div className="mc-buttons">
              {normalEvents.map((event) => (
                <button
                  key={event.kind}
                  type="button"
                  className="mc-event mc-event-normal"
                  disabled={!event.available || locked}
                  title={event.reason ?? ''}
                  onClick={() =>
                    run(
                      'event',
                      () => machineApi.event(event.kind),
                      `Ошибка события «${event.title}»`,
                    )
                  }
                >
                  {event.title}
                </button>
              ))}
            </div>
          </div>
        )}

        {faultEvents.length > 0 && (
          <div className="mc-group">
            <div className="mc-group-title">Аварийные события</div>
            <div className="mc-buttons">
              {faultEvents.map((event) => (
                <button
                  key={event.kind}
                  type="button"
                  className="mc-event mc-event-fault"
                  disabled={!event.available || locked}
                  title={event.reason ?? ''}
                  onClick={() =>
                    run(
                      'event',
                      () => machineApi.event(event.kind),
                      `Ошибка события «${event.title}»`,
                    )
                  }
                >
                  {event.title}
                </button>
              ))}
            </div>
          </div>
        )}

        {error && <div className="mc-error">{error}</div>}
      </div>
    </section>
  )
}
