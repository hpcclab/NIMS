import json
import os
from datetime import datetime, timedelta
from time import sleep

import requests
from dateutil.tz import tzlocal
from flask import abort
from requests import HTTPError, RequestException

from app.const import MIGRATION_ID_ANNOTATION, START_MODE_ANNOTATION, START_MODE_PASSIVE, \
    VOLUME_LIST_ANNOTATION, \
    SYNC_HOST_ANNOTATION, SYNC_PORT_ANNOTATION, LAST_APPLIED_CONFIG, START_MODE_NULL, \
    INTERFACE_DIND, START_MODE_ACTIVE, INTERFACE_ANNOTATION, MIGRATION_POSITION_ANNOTATION, MIGRATION_STEP_ANNOTATION, \
    MIGRATION_POSITION_DES, MIGRATION_STEP_RESERVED, MIGRATION_STEP_DELETING, MIGRATION_STEP_RESTORING
from app.env import SCRATCH_IMAGE
from app.orchestrator import select_orchestrator

client = select_orchestrator()


def get_name():
    return INTERFACE_DIND


def is_compatible(src_pod, des_info):
    try:
        response = requests.get(f"http://{src_pod['status']['podIP']}:2375/_ping")
        response.raise_for_status()
        if 'Docker' in response.headers.get('Server'):
            return True
    except RequestException:
        pass
    return False


def generate_des_pod_template(src_pod, migrate_image):
    body = json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    if migrate_image:
        for container in body['spec']['containers']:
            container['image'] = SCRATCH_IMAGE
    body['metadata']['annotations'][LAST_APPLIED_CONFIG] = json.dumps(body)
    body['metadata']['annotations'][START_MODE_ANNOTATION] = START_MODE_PASSIVE
    body['metadata']['annotations'][MIGRATION_ID_ANNOTATION] = src_pod['metadata']['annotations'][
        MIGRATION_ID_ANNOTATION]
    body['metadata']['annotations'][MIGRATION_POSITION_ANNOTATION] = MIGRATION_POSITION_DES
    body['metadata']['annotations'][MIGRATION_STEP_ANNOTATION] = MIGRATION_STEP_RESERVED
    return body


def create_des_pod(des_pod_template, des_info, migration_state):
    try:
        response = requests.post(f"http://{des_info['url']}/create", json={
            'interface': get_name(),
            'template': des_pod_template
        })
        response.raise_for_status()
        migration_state['des_pod_exist'] = True
        return response.json()
    except HTTPError as e:
        if e.response.status_code == 504:
            migration_state['des_pod_exist'] = True
        raise e


def do_create_pod(template):
    namespace = template.get('metadata', {}).get('namespace', 'default')
    new_pod = client.create_pod(namespace, template)
    msg = wait_created_pod_ready(new_pod)
    exit_code = os.system(f"/app/wait-for-it.sh {msg['ip']}:8888 -t 1")
    if exit_code == 0:
        response = requests.get(f"http://{msg['ip']}:8888/list")
        response.raise_for_status()
        return {
            **msg['annotations'],
            'current-containers': response.json()
        }
    abort(502, 'Interface does not respond to /list')


def wait_created_pod_ready(pod):
    start_time = datetime.now(tz=tzlocal())
    while True:
        if 'podIP' in pod['status']:
            status_code = probe_all(pod['status']['podIP'])
            annotations = pod['metadata']['annotations']
            if (annotations[START_MODE_ANNOTATION] == START_MODE_ACTIVE and status_code == 200) \
                    or (annotations[START_MODE_ANNOTATION] == START_MODE_PASSIVE and status_code == 204) \
                    or (annotations[START_MODE_ANNOTATION] == START_MODE_NULL and status_code < 400):
                if SYNC_HOST_ANNOTATION in annotations and SYNC_PORT_ANNOTATION in annotations:
                    return {'annotations': {
                        VOLUME_LIST_ANNOTATION: annotations[VOLUME_LIST_ANNOTATION],
                        SYNC_HOST_ANNOTATION: annotations[SYNC_HOST_ANNOTATION],
                        SYNC_PORT_ANNOTATION: annotations[SYNC_PORT_ANNOTATION]
                    }, 'ip': pod['status']['podIP']}
                else:
                    pod = client.get_pod(pod['metadata']['name'], pod['metadata']['namespace'])
        else:
            pod = client.get_pod(pod['metadata']['name'], pod['metadata']['namespace'])

        if datetime.now(tz=tzlocal()) - start_time > timedelta(minutes=1):
            abort(504, 'Timeout while waiting pod to be ready')

        sleep(0.1)


def probe_all(pod_ip):
    exit_code = os.system(f"/app/wait-for-it.sh {pod_ip}:8888 -t 1")
    if exit_code == 0:
        return requests.get(f"http://{pod_ip}:8888/probeAll").status_code
    return 1


def checkpoint_and_transfer(src_pod, des_pod_annotations, checkpoint_id, migration_state, migrate_image, destination_url, migration_id, des_pod_template):
    name = src_pod['metadata']['name']
    namespace = src_pod['metadata'].get('namespace', 'default')
    src_pod = client.update_pod_restart(name, namespace, START_MODE_NULL)
    response = requests.post(f"http://{src_pod['status']['podIP']}:8888/migrate", json={
        'checkpointId': checkpoint_id,
        'interfaceHost': des_pod_annotations[SYNC_HOST_ANNOTATION],
        'interfacePort': des_pod_annotations[SYNC_PORT_ANNOTATION],
        'containers': des_pod_annotations['current-containers'],
        'image': migrate_image is not None,
        'volumes': json.loads(des_pod_annotations[VOLUME_LIST_ANNOTATION]),
        'template': json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    })
    response.raise_for_status()
    fields = ['checkpoint', 'pre_checkpoint', 'checkpoint_files_transfer', 'checkpoint_files_delay',
              'file_system_transfer', 'file_system_delay', 'volume_transfer', 'volume_delay',
              'save_image', 'image_layers_transfer', 'image_layers_delay', 'load_image']
    checkpoint_and_transfer_overhead = {
        field: max([overhead.get(field, -1) for overhead in response.json()]) for field in fields
    }
    return src_pod, {
        field: checkpoint_and_transfer_overhead[field] if checkpoint_and_transfer_overhead[field] > -1 else None
        for field in fields
    }


def load_image(body):
    pass


def restore(body):
    name = body['name']
    namespace = body.get('namespace', 'default')
    migration_id = body['migrationId']
    des_pod = client.get_pod(name, namespace)
    if des_pod['metadata']['annotations'].get(MIGRATION_ID_ANNOTATION) != migration_id:
        abort(409, "Pod is being migrated")
    client.update_migration_step(name, namespace, MIGRATION_STEP_RESTORING)
    response = requests.post(f"http://{des_pod['status']['podIP']}:8888/restore", json={
        'checkpointId': body['checkpointId'],
        'template': body.get('template')
    })
    response.raise_for_status()
    wait_restored_pod_ready(des_pod)
    client.update_pod_restart(name, namespace, START_MODE_ACTIVE)
    return client.release_pod(name, namespace)


def wait_restored_pod_ready(pod):
    start_time = datetime.now(tz=tzlocal())
    while True:
        status_code = probe_all(pod['status']['podIP'])
        if status_code == 200:
            return

        if datetime.now(tz=tzlocal()) - start_time > timedelta(minutes=1):
            abort(504, 'Timeout while waiting pod to be ready')

        sleep(0.1)


def delete_src_pod(src_pod):
    name = src_pod['metadata']['name']
    namespace = src_pod['metadata'].get('namespace', 'default')
    client.update_migration_step(name, namespace, MIGRATION_STEP_DELETING)
    do_delete_pod(name, namespace)


def do_delete_pod(name, namespace):
    client.delete_pod(name, namespace, delete_ambassador=True)


def recover(src_pod, destination_url, migration_state, delete_frontman, delete_des_pod):
    try:
        name = src_pod['metadata']['name']
        namespace = src_pod['metadata'].get('namespace', 'default')
        if src_pod['metadata']['annotations'].get(INTERFACE_ANNOTATION) != START_MODE_ACTIVE:
            client.update_pod_restart(name, namespace, START_MODE_ACTIVE)
        if migration_state['frontmant_exist']:
            delete_frontman(src_pod)
        if migration_state['des_pod_exist']:
            delete_des_pod(src_pod, destination_url, get_name())
    except Exception:
        pass
