import axios from 'axios'
import {
  HeartbeatResponse,
  ImportResponse,
  JournalEntry,
  LoadResponse,
  PlaybackStatus,
  SaveResponse,
  ScenarioDescribe,
  ScenarioStatus,
  StateInfo,
  StationBindingsResponse,
  StationSnapshotInfo,
  WriteBatchResponse,
  WriteResponse,
} from './types'

const api = axios.create({
  baseURL: '/api',
})

export const signalsApi = {
  write: (id: string, value: boolean | number | string) =>
    api.post<WriteResponse>('/signals/write', { id, value }).then((r) => r.data),
  writeBatch: (values: Record<string, boolean | number | string>) =>
    api.post<WriteBatchResponse>('/signals/write_batch', { values }).then((r) => r.data),
}

export const statesApi = {
  list: () => api.get<StateInfo[]>('/states/list').then((r) => r.data),
  save: (name: string, description: string) =>
    api.post<SaveResponse>('/states/save', { name, description }).then((r) => r.data),
  load: (filename: string) =>
    api.post<LoadResponse>('/states/load', { filename }).then((r) => r.data),
  remove: (filename: string) =>
    api.delete(`/states/delete/${encodeURIComponent(filename)}`).then((r) => r.data),
  export: (name: string, description: string) =>
    api
      .post<Blob>('/states/export', { name, description }, { responseType: 'blob' })
      .then((r) => r.data),
  import: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api
      .post<ImportResponse>('/states/import', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data)
  },
}

export const heartbeatApi = {
  get: () => api.get<HeartbeatResponse>('/heartbeat').then((r) => r.data),
  set: (enabled: boolean) =>
    api.put<HeartbeatResponse>('/heartbeat', { enabled }).then((r) => r.data),
}

export const playbackApi = {
  status: () => api.get<PlaybackStatus>('/playback/status').then((r) => r.data),
  loadPath: (path: string) =>
    api.post<PlaybackStatus>('/playback/load_path', { path }).then((r) => r.data),
  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api
      .post<PlaybackStatus>('/playback/load', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data)
  },
  control: (action: string, extra?: { rate?: number; seek_ms?: number }) =>
    api.post<PlaybackStatus>('/playback/control', { action, ...extra }).then((r) => r.data),
  unload: () => api.post<PlaybackStatus>('/playback/unload').then((r) => r.data),
}

export const scenariosApi = {
  describe: () => api.get<ScenarioDescribe>('/scenarios').then((r) => r.data),
  status: () => api.get<ScenarioStatus>('/scenarios/status').then((r) => r.data),
  journal: (limit = 50) =>
    api.get<JournalEntry[]>(`/scenarios/journal?limit=${limit}`).then((r) => r.data),
  startMode: (mode: string) =>
    api.post<ScenarioStatus>('/scenarios/start', { mode }).then((r) => r.data),
  startKinds: (kinds: string[], once = true) =>
    api.post<ScenarioStatus>('/scenarios/start', { kinds, once }).then((r) => r.data),
  stop: () => api.post<ScenarioStatus>('/scenarios/stop').then((r) => r.data),
}

// Прокси станции: read-only белый список. 502 station_unreachable приходит
// как ошибка axios с detail.code в теле.
export const stationApi = {
  health: () => api.get<{ status?: string }>('/station/health').then((r) => r.data),
  bindings: () =>
    api.get<StationBindingsResponse>('/station/bindings').then((r) => r.data),
  snapshot: (mode = 'live_batch', projectId?: string) =>
    api
      .post<StationSnapshotInfo>('/station/snapshots', {
        mode,
        ...(projectId ? { project_id: projectId } : {}),
      })
      .then((r) => r.data),
  calculations: () =>
    api.get<{ calculations?: unknown[] }>('/station/calculations').then((r) => r.data),
}
