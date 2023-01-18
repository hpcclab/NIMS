from os import getenv

from app.const import ORCHESTRATOR_TYPE_KUBERNETES

ORCHESTRATOR_TYPE = getenv('ORCHESTRATOR_TYPE', ORCHESTRATOR_TYPE_KUBERNETES)
MARATHON_URL = getenv('MARATHON_URL', 'http://localhost:8080')
SSU_INTERFACE_ENABLE = getenv('SSU_INTERFACE_ENABLE', None)
SSU_INTERFACE_SERVICE = getenv('SSU_INTERFACE_SERVICE', 'ssu-interface')
SSU_INTERFACE_HOST = getenv('SSU_INTERFACE_HOST', None)
SSU_INTERFACE_NODEPORT = getenv('SSU_INTERFACE_NODEPORT', '30002')

DIND_IMAGE = getenv('DIND_IMAGE', 'migration-dind')
PIND_IMAGE = getenv('PIND_IMAGE', 'migration-pind')
AMBASSADOR_IMAGE = getenv('AMBASSADOR_IMAGE', 'migration-ambassador')
FRONTMAN_IMAGE = getenv('FRONTMAN_IMAGE', 'frontman')
IMAGE_PULL_POLICY = getenv('IMAGE_PULL_POLICY', 'IfNotPresent')

env = {
    'DIND_IMAGE': DIND_IMAGE,
    'PIND_IMAGE': PIND_IMAGE,
    'AMBASSADOR_IMAGE': AMBASSADOR_IMAGE,
    'FRONTMAN_IMAGE': FRONTMAN_IMAGE,
    'IMAGE_PULL_POLICY': IMAGE_PULL_POLICY
}
