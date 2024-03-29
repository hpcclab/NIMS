ORCHESTRATOR_TYPE_KUBERNETES = 'kubernetes'
ORCHESTRATOR_TYPE_MINISHIFT = 'minishift'
ORCHESTRATOR_TYPE_MESOS = 'mesos'
LAST_APPLIED_CONFIG = 'kubectl.kubernetes.io/last-applied-configuration'
DOMAIN = 'migration'
MIGRATABLE_ANNOTATION = f'{DOMAIN}-migratable'
MIGRATABLE_TRUE = str(True)
MIGRATABLE_POSSIBLE = 'Possible'
MIGRATABLE_FALSE = str(False)
BYPASS_ANNOTATION = f'{DOMAIN}-bypass'
MIGRATION_ID_ANNOTATION = f'{DOMAIN}-id'
CONTAINER_SPEC_ANNOTATION = f'{DOMAIN}-containers'
VOLUME_LIST_ANNOTATION = f'{DOMAIN}-volumes'
SYNC_HOST_ANNOTATION = f'{DOMAIN}-host'
SYNC_PORT_ANNOTATION = f'{DOMAIN}-port'
START_MODE_ANNOTATION = f'{DOMAIN}-start-mode'
START_MODE_ACTIVE = 'active'
START_MODE_PASSIVE = 'passive'
START_MODE_NULL = 'null'
START_MODE_FAIL = 'fail'
INTERFACE_ANNOTATION = f'{DOMAIN}-interface'
INTERFACE_DIND = 'dind'
INTERFACE_PIND = 'pind'
INTERFACE_FF = 'ff'
