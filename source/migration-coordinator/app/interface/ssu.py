import json

import requests

from app.const import MIGRATION_ID_ANNOTATION, SYNC_HOST_ANNOTATION, SYNC_PORT_ANNOTATION, LAST_APPLIED_CONFIG, \
    INTERFACE_SSU, MIGRATION_POSITION_ANNOTATION, MIGRATION_POSITION_DES, MIGRATION_STEP_ANNOTATION, \
    MIGRATION_STEP_RESTORING, VOLUME_LIST_ANNOTATION
from app.env import SSU_INTERFACE_SERVICE, SSU_INTERFACE_ENABLE
from app.orchestrator import select_orchestrator

client = select_orchestrator()


def get_name():
    return INTERFACE_SSU


def is_compatible(src_pod, des_info):
    if 'ssu_port' in des_info and SSU_INTERFACE_ENABLE is not None:
        return True
    return False


def generate_des_pod_template(src_pod, migrate_image):
    body = json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    body['metadata']['annotations'][LAST_APPLIED_CONFIG] = src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG)
    body['metadata']['annotations'][MIGRATION_ID_ANNOTATION] = src_pod['metadata']['annotations'][
        MIGRATION_ID_ANNOTATION]
    body['metadata']['annotations'][MIGRATION_POSITION_ANNOTATION] = MIGRATION_POSITION_DES
    body['metadata']['annotations'][MIGRATION_STEP_ANNOTATION] = MIGRATION_STEP_RESTORING
    return body


def create_des_pod(des_pod_template, des_info, migration_state):
    return {SYNC_HOST_ANNOTATION: des_info['ssu_host'], SYNC_PORT_ANNOTATION: des_info['ssu_port']}


def do_create_pod(template):
    namespace = template.get('metadata', {}).get('namespace', 'default')
    client.create_pod(namespace, template)
    return {
        'current-containers': None
    }


def checkpoint_and_transfer(src_pod, des_pod_annotations, checkpoint_id, migration_state, migrate_image, destination_url, migration_id, des_pod_template):
    response = requests.post(f"http://{SSU_INTERFACE_SERVICE}:8888/migrate", json={
        'checkpointId': checkpoint_id,
        'interfaceHost': des_pod_annotations[SYNC_HOST_ANNOTATION],
        'interfacePort': des_pod_annotations[SYNC_PORT_ANNOTATION],
        'containers': [],
        'image': False,
        'volumes': [],  # todo check if volume is migrated
        'template': json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    })
    response.raise_for_status()  # todo forward body and add migration id, checkpoint id
    migration_state['src_pod_exist'] = False
    client.delete_ssu_custom_resource(checkpoint_id, src_pod['metadata'].get('namespace', 'default'))
    response_body = response.json()
    fields = ['checkpoint', 'pre_checkpoint', 'checkpoint_files_transfer', 'checkpoint_files_delay',
              'file_system_transfer', 'file_system_delay', 'volume_transfer', 'volume_delay',
              'save_image', 'image_layers_transfer', 'image_layers_delay', 'load_image']
    return src_pod, {
        field: response_body.get(field) for field in fields
    }


def load_image(body):
    pass


def restore(body):
    namespace = body.get('namespace', 'default')
    migration_id = body['migrationId']
    checkpoint_id = body['checkpointId']
    des_pod = body.get('template')
    response = requests.post(f"http://{SSU_INTERFACE_SERVICE}:8888/restore", json={
        'checkpointId': checkpoint_id,
        'template': des_pod
        # 'volumes': json.loads(des_pod_annotations[VOLUME_LIST_ANNOTATION])
        # todo check if volume is migrated
    })
    response.raise_for_status()
    pod_name = client.wait_restored_pod_ready_ssu(namespace, migration_id)
    client.delete_pod_owner_reference(pod_name, namespace, checkpoint_id)
    client.delete_ssu_custom_resource(checkpoint_id, namespace)
    return client.release_pod(pod_name, namespace)


def delete_src_pod(src_pod):
    pass


def do_delete_pod(name, namespace):
    client.delete_pod(name, namespace)


def recover(src_pod, destination_url, migration_state, delete_frontman, delete_des_pod):
    try:
        if not migration_state['src_pod_exist']:
            do_create_pod(generate_des_pod_template(src_pod))  # todo check if it is going to be deleted / deleting
        if migration_state['frontmant_exist']:
            delete_frontman(src_pod)
        if migration_state['des_pod_exist']:
            delete_des_pod(src_pod, destination_url, get_name())
    except Exception:
        pass  # todo forward body and add migration id, checkpoint id
