export const RUNTIME_ENGINE_VALUES = ['in_process', 'sandboxed'] as const

export type RuntimeEngine = (typeof RUNTIME_ENGINE_VALUES)[number]

export const RUNTIME_ENGINE_LABELS: Record<RuntimeEngine, string> = {
  in_process: 'In-process',
  sandboxed: 'Sandboxed',
}
