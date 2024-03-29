import json
import os
from concurrent.futures import wait, ThreadPoolExecutor, FIRST_EXCEPTION
from datetime import datetime, timedelta
from time import sleep

import requests
from dateutil.tz import tzlocal
from flask import abort
from requests import HTTPError, RequestException

from app.const import MIGRATION_ID_ANNOTATION, START_MODE_ANNOTATION, VOLUME_LIST_ANNOTATION, \
    SYNC_HOST_ANNOTATION, SYNC_PORT_ANNOTATION, LAST_APPLIED_CONFIG, START_MODE_NULL, \
    INTERFACE_PIND, START_MODE_ACTIVE, START_MODE_PASSIVE, INTERFACE_ANNOTATION, MIGRATION_POSITION_ANNOTATION, \
    MIGRATION_STEP_ANNOTATION, MIGRATION_POSITION_DES, MIGRATION_STEP_RESERVED, MIGRATION_STEP_DELETING, \
    MIGRATION_STEP_RESTORING
from app.orchestrator import select_orchestrator

client = select_orchestrator()


def get_name():
    return INTERFACE_PIND


def is_compatible(src_pod, des_info):
    try:
        response = requests.get(f"http://{src_pod['status']['podIP']}:2375/_ping")
        response.raise_for_status()
        if 'Libpod' in response.headers.get('Server'):
            return True
    except RequestException:
        pass
    return False


def generate_des_pod_template(src_pod, migrate_image):
    body = json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    body['metadata']['annotations'][LAST_APPLIED_CONFIG] = src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG)
    body['metadata']['annotations'][START_MODE_ANNOTATION] = START_MODE_NULL
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
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(checkpoint_and_transfer_container, src_pod, des_pod_annotations, checkpoint_id),
                   executor.submit(checkpoint_and_transfer_image, migrate_image, name, namespace, src_pod, des_pod_annotations, checkpoint_id, destination_url, migration_id, des_pod_template)]
    done, _ = wait(futures, return_when=FIRST_EXCEPTION)
    for task in done:
        err = task.exception()
        if err is not None:
            raise err
    container_overheads, image_overheads = futures[0].result(), futures[1].result()
    fields = ['checkpoint', 'pre_checkpoint', 'checkpoint_files_transfer', 'checkpoint_files_delay',
              'file_system_transfer', 'file_system_delay', 'volume_transfer', 'volume_delay']
    checkpoint_and_transfer_overhead = {
        field: max([overhead.get(field, -1) for overhead in container_overheads]) for field in fields
    }
    for field in ['save_image', 'image_layers_transfer', 'image_layers_delay', 'load_image']:
        checkpoint_and_transfer_overhead[field] = max([overhead.get(field, -1) for overhead in image_overheads])
    return src_pod, {
        field: checkpoint_and_transfer_overhead[field] if checkpoint_and_transfer_overhead[field] > -1 else None
        for field in fields
    }


def checkpoint_and_transfer_container(src_pod, des_pod_annotations, checkpoint_id):
    response = requests.post(f"http://{src_pod['status']['podIP']}:8888/migrate", json={
        'checkpointId': checkpoint_id,
        'interfaceHost': des_pod_annotations[SYNC_HOST_ANNOTATION],
        'interfacePort': des_pod_annotations[SYNC_PORT_ANNOTATION],
        'containers': des_pod_annotations['current-containers'],
        'image': False,
        'volumes': json.loads(des_pod_annotations[VOLUME_LIST_ANNOTATION]),
        'template': json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    })
    response.raise_for_status()
    return response.json()


def checkpoint_and_transfer_image(migrate_image, name, namespace, src_pod, des_pod_annotations, checkpoint_id, destination_url, migration_id, des_pod_template):
    if migrate_image is None:
        return [{}]
    response = requests.post(f"http://{src_pod['status']['podIP']}:8888/save", json={
        'checkpointId': checkpoint_id,
        'interfaceHost': des_pod_annotations[SYNC_HOST_ANNOTATION],
        'interfacePort': des_pod_annotations[SYNC_PORT_ANNOTATION],
        'containers': des_pod_annotations['current-containers'],
        'volumes': json.loads(des_pod_annotations[VOLUME_LIST_ANNOTATION]),
        'template': json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    })
    response.raise_for_status()
    results = response.json()
    start = datetime.now(tz=tzlocal())
    response = requests.post(f"http://{destination_url}/image", json={
        'migrationId': migration_id,
        'checkpointId': checkpoint_id,
        'name': name,
        'namespace': namespace,
        'interface': INTERFACE_PIND,
        'template': des_pod_template
    })
    response.raise_for_status()
    load_image = (datetime.now(tz=tzlocal()) - start).total_seconds()
    for result in results:
        result['load_image'] = load_image
    return results


def load_image(body):
    name = body['name']
    namespace = body.get('namespace', 'default')
    migration_id = body['migrationId']
    des_pod = client.get_pod(name, namespace)
    if des_pod['metadata']['annotations'].get(MIGRATION_ID_ANNOTATION) != migration_id:
        abort(409, "Pod is being migrated")
    response = requests.post(f"http://{des_pod['status']['podIP']}:8888/load", json={
        'checkpointId': body['checkpointId'],
        'template': body.get('template')
    })
    response.raise_for_status()


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
