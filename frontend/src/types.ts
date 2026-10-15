export interface StateInfo {
  filename: string
  name: string
  description: string
  created: string
  size: number
  signal_count: number
}

export interface SaveResponse {
  status: string
  filename: string
  message: string
  signal_count?: number
}

export interface LoadResponse {
  status: string
  loaded_count: number
  errors: string[]
  message: string
}

export interface ImportResponse {
  status: string
  filename: string
  signal_count: number
  errors?: string[]
  message: string
}

export interface Signal {
  id: string
  short_id?: string
  name: string
  type: string
  writable: boolean
  value: boolean | number | string | null
  /** Ревизия подтверждённого состояния (ТЗ #5 §6.3); растёт с каждой записью. */
  revision?: number
  /** Кто держит сигнал сейчас (ТЗ #5 §9.2), если держит. */
  owner?: string | null
  owner_id?: string | null
  direction?: string
  channel?: string
  module?: string
  cabinet?: string
  device?: string
  address?: string
  project_tag?: string
  bit?: string | number
  word_address?: string | number
  logic?: string
}

export interface WriteResponse {
  status: string
  id: string
  value: boolean | number | string
}

/** Результат одной команды пакета (ТЗ #5 §7): применён или причина отказа. */
export interface WriteResultEntry {
  status: 'applied' | 'rejected' | 'not_applied' | 'failed'
  value?: boolean | number | string
  revision?: number
  code?: string
  error?: string
}

export interface WriteBatchResponse {
  status: string
  applied: string[]
  errors: string[]
  /** Подтверждённые значения по каждому сигналу; рендерим только подтверждённое (§8). */
  results?: Record<string, WriteResultEntry>
  op_id?: string
  replayed?: boolean
}

export type MessageKind = 'success' | 'error' | 'info'

export interface AppMessage {
  kind: MessageKind
  text: string
}

export interface HeartbeatResponse {
  enabled: boolean
  supported: boolean
}

export interface RecordedMode {
  mode: string
  title: string
  fault: boolean
  description: string
  expected_answer: string
  signals: string[]
  changes: number
  duration_ms: number
  source: string
  sha256: string
}

export interface ScenarioStatus {
  running: boolean
  phase: string
  run_id: string | null
  seed: number | null
  kinds: string[] | null
  once: boolean | null
  episode: number
  kind: string | null
  started_at: string | null
  writes: number
  write_failures: number
  max_lag_ms: number
  error: string | null
}

export interface ScenarioDescribe {
  available: boolean
  unavailable_reason?: string | null
  missing_signals?: string[]
  kinds: { kind: string; title: string; fault: boolean }[]
  modes: RecordedMode[]
  assumptions: { signal_id: string; value: unknown; note: string }[]
  status: ScenarioStatus
}

export interface JournalCause {
  signal_id: string
  name: string | null
  project_tag: string | null
  before: unknown
  after: unknown
}

export interface JournalEntry {
  run_id: string
  seed: number
  episode: number
  kind: string
  title: string
  fault: boolean
  causes: JournalCause[]
  cause_at: string
  stopped_at: string
  recovered_at: string
  cause_visible_after_stop: boolean
  expected_answer: string
}

export interface PlaybackStatus {
  loaded: boolean
  source: string | null
  // Момент первой строки тренда (ISO); известен сразу после загрузки.
  t0: string | null
  // Участок прогона в мс от начала тренда; конец включителен.
  start_ms: number
  end_ms: number | null
  phase: string
  mapped_signals: number
  unmatched_columns: number
  ambiguous: number
  position_ms: number
  duration_ms: number | null
  rate: number
  paused: boolean
  // Заморозка таймлайна (ТЗ #5): false — плейбэк не пишет в сигналы,
  // пуск/переход отклоняются сервером.
  writes_enabled: boolean
  rows_played: number
  writes: number
  write_failures: number
  max_lag_ms: number
  error: string | null
}

// -- станция (эхо через прокси имитатора) ------------------------------------

export type TelemetryBindingState =
  | 'active'
  | 'inactive'
  | 'connection_missing'
  | 'error'
  | 'bad_quality'
  | 'unbound'

/** Строка таблицы сигналов станции (src/api/schemas.py TelemetryBindingRow). */
export interface TelemetryBindingRow {
  id: string
  project_id: string
  project_tag_id: string
  name: string
  channel?: string | null
  cabinet?: string | null
  module?: string | null
  device?: string | null
  direction?: string | null
  bit?: number | null
  bound: boolean
  state: TelemetryBindingState
  last_value?: boolean | number | string | null
  last_quality?: string | null
  last_source_timestamp?: string | null
  symbol_id?: string | null
}

export interface StationBindingsResponse {
  bindings: TelemetryBindingRow[]
}

/** Ответ станции на снимок/расчёт: читаем свободно — контракт широкий. */
export interface StationSnapshotInfo {
  id: string
  mode?: string
  status?: string
  project_id?: string
  created_at?: string
  [key: string]: unknown
}

// -- сверка с журналом правды -------------------------------------------------

export type VerificationVerdict = 'pass' | 'partial' | 'fail'

export interface VerificationReport {
  generated_at?: string
  entry: {
    run_id?: string
    seed?: number
    episode?: number
    kind?: string
    title?: string
    fault?: boolean
    cause_at?: string
    stopped_at?: string
  }
  snapshot: {
    id?: string
    capture_mode?: string
    tags_total?: number
    tags_received?: number
    value_spread_ms?: number | null
  }
  /** Переснятия снимка из-за протухших точек (ключ отсутствует, если гейт молчал). */
  snapshot_recaptures?: number
  /** Точки, которых станция не видела дольше окна до снимка (честный факт станции). */
  stale_points?: string[]
  cause_check: {
    verdicts: { signal_id: string; name?: string | null; verdict: string }[]
    counts: Record<string, number>
  }
  state_check: {
    total: number
    present: number
    missing: number
    value_matched: number
    value_mismatched: number
  }
  outputs: {
    total: number
    computed: number
    by_status: Record<string, number>
    reason_breakdown: Record<string, number>
    coverage?: number
    /** Отказ расчёта станции (факт станции, вердикт не трогает). */
    refusal?: unknown
  } | null
  outputs_error?: { status: number; detail: unknown }
  verdict: VerificationVerdict
}

export interface VerificationListItem {
  file: string
  generated_at?: string
  verdict: VerificationVerdict
  kind?: string | null
  title?: string | null
  run_id?: string | null
  episode?: number | null
  cause_counts?: Record<string, number>
  state_mismatched?: number
}

export interface VerificationListResponse {
  items: VerificationListItem[]
  total: number
}

// -- схема Э3 (листы станции через прокси + мост DT→сигналы) ------------------

/** Аппарат моста ``stand.schematic-bridge.v1``: сигналы стенда и точки листов. */
export interface SchematicBridgeDevice {
  signals: string[]
  /** Номер листа → [x%, y%] бейджа (доли ширины/высоты листа). */
  sheets: Record<string, [number, number]>
}

export interface SchematicBridgeUnmatched {
  signal_id: string
  reason: 'no_designator' | 'not_on_sheets'
  dt: string | null
}

export interface SchematicBridge {
  schema: string
  devices: Record<string, SchematicBridgeDevice>
  unmatched_signals: SchematicBridgeUnmatched[]
  coverage: {
    signals_total: number
    signals_matched: number
    devices: number
    sheets: number
  }
}

/** Строка списка листов станции (SchematicPageBrief, src/api/schematic.py). */
export interface StationSchematicPageBrief {
  number: number
  sheet_designation?: string
  description?: string
  has_raster?: boolean
  [key: string]: unknown
}

export interface StationSchematicPagesResponse {
  count: number
  pages: StationSchematicPageBrief[]
}

// -- архив станции (окно для плейбэка) ----------------------------------------

export interface ArchiveSeriesInfo {
  signal_id: string
  value_type: string
  rows: number
  observed_min_us: number
  observed_max_us: number
}

export interface ArchiveSeriesResponse {
  path: string
  observed_min_us: number | null
  observed_max_us: number | null
  total_rows: number
  series: ArchiveSeriesInfo[]
}

export interface LoadArchiveRequest {
  signal_ids?: string[] | null
  start_us?: number | null
  end_us?: number | null
}
