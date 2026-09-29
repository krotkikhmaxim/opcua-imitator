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

export interface WriteBatchResponse {
  status: string
  applied: string[]
  errors: string[]
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
