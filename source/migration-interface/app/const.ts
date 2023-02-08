export const HOST = process.env.DOCKER_HOST || process.env.HOST || '127.0.0.1:2375'
export const SCRATCH_IMAGE = process.env.SCRATCH_IMAGE || 'nims/scratch'
export const LOG_LEVEL = process.env.LOG_LEVEL || 'info'
export const DOMAIN = 'migration'
export const SPEC_CONTAINER_ANNOTATION = `${DOMAIN}-containers`
export const START_MODE_ANNOTATION = `${DOMAIN}-start-mode`
export const START_MODE_ACTIVE = 'active'
export const START_MODE_PASSIVE = 'passive'
export const START_MODE_NULL = 'null'
export const START_MODE_FAIL = 'fail'
export const INTERFACE_ANNOTATION = 'migration-interface'
export const INTERFACE_DIND = 'dind'
export const INTERFACE_PIND = 'pind'
export const INTERFACE_FF = 'ff'
export const INTERFACE_SSU = 'ssu'