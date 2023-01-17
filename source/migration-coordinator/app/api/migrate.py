import asyncio
import json
from datetime import datetime
from os import path
from uuid import uuid4

import requests
import yaml
from dateutil.tz import tzlocal
from flask import Blueprint, request, abort

from app.const import MIGRATABLE_ANNOTATION, MIGRATION_ID_ANNOTATION, START_MODE_ANNOTATION, START_MODE_ACTIVE, \
    START_MODE_PASSIVE, INTERFACE_ANNOTATION, INTERFACE_DIND, VOLUME_LIST_ANNOTATION, \
    SYNC_HOST_ANNOTATION, SYNC_PORT_ANNOTATION, LAST_APPLIED_CONFIG, ORCHESTRATOR_TYPE_MESOS, INTERFACE_PIND, \
    INTERFACE_FF, START_MODE_NULL, BYPASS_ANNOTATION
from app.env import env, FRONTMAN_IMAGE, ORCHESTRATOR_TYPE, SSU_INTERFACE_SERVICE
from app.kubernetes_client import create_pod, update_pod_label, wait_pod_ready
from app.lib import get_information, gather, get_pod, lock_pod, release_pod, update_pod_restart, update_pod_redirect,\
    delete_pod, exec_pod, check_error_event

migrate_api_blueprint = Blueprint('migrate_api', __name__)


# todo disallowed

@migrate_api_blueprint.route("/migrate", methods=['POST'])
def migrate_api():
    body = request.get_json()

    name = body.get('name')
    if name is None:
        abort(400, 'name is null')

    destination_url = body.get('destinationUrl')
    if destination_url is None:
        abort(400, 'destinationUrl is null')

    migration_id = uuid4().hex[:8]

    start_time, created_time, checkpointed_time, restored_time = migrate(body, migration_id)

    return f"migration complete! id: {migration_id}, {start_time}, {created_time}, {checkpointed_time}, {restored_time}"


def migrate(body, migration_id):
    # todo measure "ping api" latency
    start_time = datetime.now(tz=tzlocal())
    name = body['name']
    namespace = body.get('namespace', 'default')
    destination_url = body['destinationUrl']
    keep = False
    last_checked_time = datetime.now(tz=tzlocal())
    src_pod = get_pod(name, namespace)
    if not bool(src_pod['metadata']['annotations'].get(MIGRATABLE_ANNOTATION)):
        abort(400, "pod is not migratable")
    if src_pod['metadata']['annotations'][START_MODE_ANNOTATION] != START_MODE_ACTIVE:
        abort(400, "Pod is not migratable")
    if src_pod['metadata']['annotations'].get(MIGRATION_ID_ANNOTATION):
        abort(409, "Pod is being migrated")
    # last_checked_time = abort_if_error_exists(migration_id, name, namespace, last_checked_time)
    src_pod = lock_pod(name, namespace, migration_id)
    try:
        # last_checked_time = abort_if_error_exists(migration_id, name, namespace, last_checked_time)
        des_info = ping_destination(destination_url)
        # last_checked_time = abort_if_error_exists(migration_id, name, namespace, last_checked_time)
        if src_pod['metadata']['annotations'].get(INTERFACE_ANNOTATION) in [INTERFACE_DIND, INTERFACE_PIND, INTERFACE_FF]:
            des_pod_annotations = create_des_pod(src_pod, destination_url)
        else:
            des_pod_annotations = des_info
        # last_checked_time = abort_if_error_exists(migration_id, name, namespace, last_checked_time)
        # todo try deleting des pod if timeout
        created_time = datetime.now(tz=tzlocal())
        if body.get('keep'):
            keep = create_frontman(src_pod)
        try:
            checkpoint_id = uuid4().hex[:8]
            # todo actual checkpoint time (interface)
            src_pod = checkpoint_and_transfer(src_pod, des_pod_annotations, checkpoint_id)
            # _ = abort_if_error_exists(migration_id, name, namespace, last_checked_time)
        except Exception as e:
            if src_pod['metadata']['annotations'].get(INTERFACE_ANNOTATION) in [INTERFACE_DIND, INTERFACE_PIND, INTERFACE_FF]:
                delete_des_pod(src_pod, destination_url)
            raise e
        checkpointed_time = datetime.now(tz=tzlocal())
        restore_and_release_des_pod(src_pod, destination_url, migration_id, checkpoint_id)
        # todo try deleting des pod if timeout
    except Exception as e:
        if keep:
            delete_frontman(src_pod)
        raise e
    finally:
        if src_pod['metadata']['annotations'].get(INTERFACE_ANNOTATION) in [INTERFACE_DIND, INTERFACE_PIND]:
            update_pod_restart(name, namespace, START_MODE_ACTIVE)
        release_pod(name, namespace)
    try:
        if body.get('redirect'):
            if keep:
                update_frontman(src_pod, body.get('redirect'))
            else:
                create_frontman(src_pod, body.get('redirect'))
        if src_pod['metadata']['annotations'].get(INTERFACE_ANNOTATION) in [INTERFACE_DIND, INTERFACE_PIND, INTERFACE_FF]:
            delete_pod(name, namespace)
        if ORCHESTRATOR_TYPE == ORCHESTRATOR_TYPE_MESOS \
                and src_pod['metadata']['annotations'].get(INTERFACE_ANNOTATION) in [INTERFACE_DIND, INTERFACE_PIND]:
            delete_pod(f"{name}-monitor", namespace)
    except Exception as e:
        abort(500, f"Error occurs at post-migration step: {e}")
    restored_time = datetime.now(tz=tzlocal())
    return start_time, created_time, checkpointed_time, restored_time


# def abort_if_error_exists(migration_id, name, namespace, last_checked_time):
#     cur = get_db().execute("SELECT * FROM message WHERE migration_id = ?", (migration_id,))
#     rv = cur.fetchall()
#     if rv:
#         abort(rv[0]['message'])
#     return check_error_event(name, namespace, last_checked_time)
# todo check if pod restart: checkpoint step and restore step


def ping_destination(destination_url):
    response = requests.get(f"http://{destination_url}")
    response.raise_for_status()
    if response.text != get_information():  # todo check native support
        abort(409, 'Cannot migrate to incompatible destination')
    #todo fix mockup
    return {SYNC_HOST_ANNOTATION: '10.131.36.34.nip.io', SYNC_PORT_ANNOTATION: '30002'}


def create_des_pod(src_pod, destination_url):
    # todo dummy create for native
    body = json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    body['metadata']['annotations'][LAST_APPLIED_CONFIG] = src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG)
    if body['metadata']['annotations'].get(INTERFACE_ANNOTATION) == INTERFACE_PIND:
        body['metadata']['annotations'][START_MODE_ANNOTATION] = START_MODE_NULL
    else:
        body['metadata']['annotations'][START_MODE_ANNOTATION] = START_MODE_PASSIVE
    body['metadata']['annotations'][MIGRATION_ID_ANNOTATION] = src_pod['metadata']['annotations'][
        MIGRATION_ID_ANNOTATION]
    response = requests.post(f"http://{destination_url}/create", json=body)
    response.raise_for_status()
    return response.json()


def checkpoint_and_transfer(src_pod, des_pod_annotations, checkpoint_id):
    name = src_pod['metadata']['name']
    namespace = src_pod['metadata'].get('namespace', 'default')
    if src_pod['metadata']['annotations'].get(INTERFACE_ANNOTATION) in [INTERFACE_DIND, INTERFACE_PIND]:
        src_pod = update_pod_restart(name, namespace, START_MODE_NULL)
        checkpoint_and_transfer_dind(src_pod, checkpoint_id, des_pod_annotations)
    elif src_pod['metadata']['annotations'].get(INTERFACE_ANNOTATION) == INTERFACE_FF:
        checkpoint_and_transfer_ff(src_pod, des_pod_annotations)
    else:
        # todo check native support
        checkpoint_and_transfer_native(src_pod, checkpoint_id, des_pod_annotations)
    return src_pod


# todo sort and move ff-based and frontman to source dir
def checkpoint_and_transfer_native(src_pod, checkpoint_id, des_pod_annotations):
    response = requests.post(f"http://{SSU_INTERFACE_SERVICE}:8888/migrate", json={
        'checkpointId': checkpoint_id,
        'interfaceHost': des_pod_annotations[SYNC_HOST_ANNOTATION],
        'interfacePort': des_pod_annotations[SYNC_PORT_ANNOTATION],
        'template': json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
        # 'volumes': json.loads(des_pod_annotations[VOLUME_LIST_ANNOTATION])
        #todo check if volume is migrated
    })
    response.raise_for_status()
    # todo forward body
    # todo remove custom resource


def checkpoint_and_transfer_ff(src_pod, des_pod_annotations):
    volume_list = json.loads(src_pod['metadata']['annotations'][VOLUME_LIST_ANNOTATION])
    interface_host = des_pod_annotations[SYNC_HOST_ANNOTATION]
    interface_port = json.loads(des_pod_annotations[SYNC_PORT_ANNOTATION])
    name = src_pod['metadata']['name']
    namespace = src_pod['metadata'].get('namespace', 'default')
    asyncio.run(gather([exec_pod(
        name,
        namespace,
        f'''
        mc alias set migration http://{interface_host}:{interface_port[container['name']]} minioadmin minioadmin &&
        S3_CMD='/root/s3 migration' fastfreeze checkpoint --leave-running {'--preserve-path' + volume_list[container['name']] if container['name'] in volume_list else ''}
        ''',
        container['name'],
    ) for container in src_pod['spec']['containers']]))


def checkpoint_and_transfer_dind(src_pod, checkpoint_id, des_pod_annotations):
    response = requests.post(f"http://{src_pod['status']['podIP']}:8888/migrate", json={
        'checkpointId': checkpoint_id,
        'interfaceHost': des_pod_annotations[SYNC_HOST_ANNOTATION],
        'interfacePort': des_pod_annotations[SYNC_PORT_ANNOTATION],
        'containers': des_pod_annotations['current-containers'],
        'volumes': json.loads(des_pod_annotations[VOLUME_LIST_ANNOTATION])
    })
    response.raise_for_status()


def delete_des_pod(src_pod, destination_url):
    name = src_pod['metadata']['name']
    namespace = src_pod['metadata'].get('namespace', 'default')
    response = requests.post(f'http://{destination_url}/delete',
                             json={'name': name, 'namespace': namespace})
    response.raise_for_status()


def restore_and_release_des_pod(src_pod, destination_url, migration_id, checkpoint_id):
    name = src_pod['metadata']['name']
    namespace = src_pod['metadata'].get('namespace', 'default')
    response = requests.post(f"http://{destination_url}/restore",
                             json={'migrationId': migration_id,
                                   'checkpointId': checkpoint_id,
                                   'name': name,
                                   'namespace': namespace,
                                   'native': src_pod['metadata']['annotations'].get(INTERFACE_ANNOTATION) not in [INTERFACE_DIND, INTERFACE_PIND, INTERFACE_FF],
                                   'template': json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))})
    response.raise_for_status()


def create_frontman(src_pod, redirect_uri=None):
    with open(path.join(path.dirname(__file__), '../frontman.yml'), 'rt') as f:
        frontman_template = yaml.safe_load(f.read().format(**env))
    src = json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    frontman_template['metadata'] = src['metadata']
    frontman_template['metadata']['name'] += '-frontman'
    frontman_template['metadata']['annotations'].pop(MIGRATABLE_ANNOTATION, None)
    frontman_template['metadata']['annotations'].pop(INTERFACE_ANNOTATION, None)
    frontman_template['metadata']['annotations'][BYPASS_ANNOTATION] = str(True)
    if redirect_uri:
        frontman_template['metadata']['annotations']['redirect'] = redirect_uri

    frontman_template['spec']['containers'] = [{
        'name': f"{container['name']}-{port['containerPort']}",
        'image': FRONTMAN_IMAGE,
        'imagePullPolicy': 'Always',
        'ports': [{'name': 'web', 'protocol': 'TCP', 'containerPort': port['containerPort']}],
        'env': [
            {'name': 'FLASK_RUN_PORT', 'value': str(port['containerPort'])}
        ],
        'volumeMounts': [
            {'mountPath': '/etc/podinfo', 'name': 'podinfo'}
        ]
    } for container in src['spec']['containers'] for port in container.get('ports', [])]

    if not frontman_template['spec']['containers']:
        return False

    wait_pod_ready(create_pod(src['metadata']['namespace'], frontman_template))

    update_pod_label(src['metadata']['name'], src['metadata']['namespace'],
                     {k: None for k in src['metadata']['labels']})
    return True


def update_frontman(src_pod, redirect_uri):
    update_pod_redirect(f"{src_pod['metadata']['name']}-frontman", src_pod['metadata']['namespace'], redirect_uri)


def delete_frontman(src_pod):
    src = json.loads(src_pod['metadata']['annotations'].get(LAST_APPLIED_CONFIG))
    update_pod_label(src['metadata']['name'], src['metadata']['namespace'], src['metadata']['labels'])
    delete_pod(f"{src['metadata']['name']}-frontman", src['metadata']['namespace'])
