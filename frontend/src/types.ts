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
  name: string
  type: string
  writable: boolean
  value: boolean | number | string | null
}

export type MessageKind = 'success' | 'error' | 'info'

export interface AppMessage {
  kind: MessageKind
  text: string
}
