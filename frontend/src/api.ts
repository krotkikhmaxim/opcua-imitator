import axios from 'axios'
import { ImportResponse, LoadResponse, SaveResponse, StateInfo } from './types'

const api = axios.create({
  baseURL: '/api',
})

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
