#!/usr/bin/env python

import base64
import flask
import gevent.pywsgi
import kubernetes
import kubernetes.client.rest
import logging
import os
import prometheus_client
import random
import re
import socket
import sys
import string
import threading
import time

api = flask.Flask('rest')

stat_config_state = prometheus_client.Enum(
    'config_state',
    'Configuration state',
    ['name'],
    states=['unknown','provisioned','changed','failed','removed']
)
stat_webhook_call_count = prometheus_client.Counter(
    'webhook_call_count',
    'Number of webhook calls',
    ['name']
)

config_queue = None
namespace = None
pod_ip = socket.gethostbyname(os.environ['HOSTNAME'])
provision_configs = {}
default_retry_interval = os.environ.get('RETRY_INTERVAL', '10m')
default_run_interval = os.environ.get('RUN_INTERVAL', '30m')
kube_api = None
logger = None
provision_start_release = threading.Lock()
runner_image = os.environ.get(
    'RUNNER_IMAGE',
    'docker.io/gnuthought/openshift-provision-runner:latest'
)
service_account_token = None

class ProvisionConfigInvalidError(Exception):
    pass
class ProvisionConfigRemovedError(Exception):
    pass

def is_truthy(s):
    return s in ['yes', 'Yes', 'true', 'True']

def random_string(n):
    return ''.join(
        random.choice(
            string.ascii_lowercase +
            string.digits
        ) for _ in range(n)
    )

def time_to_sec(t):
    if sys.version_info < (3,):
        integer_types = (int, long,)
    else:
        integer_types = (int,)
    if isinstance(t, integer_types):
        return t
    m = re.match(r'(\d+)([mhs])?$', str(t))
    if not m:
        raise ProvisionConfigInvalidError('Invalid time unit {}'.format(t))
    if m.group(2) == 'h':
        return 3600 * int(m.group(1))
    elif m.group(2) == 'm':
        return 60 * int(m.group(1))
    else:
        return int(m.group(1))

class ProvisionConfig:
    def __init__(self, name):
        self.config_data = {}
        self.change_yaml = '# unknown'
        self.last_run_time = 0
        self.lock = threading.RLock()
        self.name = name
        self.next_time_no_check_mode = False
        self.pod_name = None
        self.pod_running = False
        self.state = 'unknown'

    def _lock(self):
        logger.debug("Acquiring lock for {}".format(self.name))
        self.lock.acquire()
        logger.debug("Acquired lock for {}".format(self.name))

    def _release(self):
        logger.debug("Releasing lock for {}".format(self.name))
        self.lock.release()
        logger.debug("Released lock for {}".format(self.name))

    def provision(self):
        self.configmap_refresh()
        self.start_run()

    def configmap_refresh(self):
        try:
            self.config_data = kube_api.read_namespaced_config_map(
                self.name,
                namespace
            ).data
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                raise ProvisionConfigRemovedError
            else:
                raise

    def check_mode(self):
        if self.next_time_no_check_mode:
            self.next_time_no_check_mode = False
            return False
        return is_truthy(self.config_data.get('check_mode','no'))

    def retry_interval(self):
        return self.config_data.get('retry_interval', default_retry_interval)

    def run_interval(self):
        return self.config_data.get('run_interval', default_run_interval)

    def set_next_time_no_check_mode(self):
        self.next_time_no_check_mode = True

    def service_account(self):
        if 'service_account' in self.config_data:
            return self.config_data['service_account']
        return self.name

    def callback_url(self):
        '''Return URL for runner pod callback'''
        return 'http://{}:5000/callback/{}/{}'.format(
            pod_ip,
            self.name,
            self.callback_key()
        )

    def callback_key(self):
        '''Get callback key value or generate value if needed'''
        if 'callback_key' in self.config_data:
            return self.config_data['callback_key']
        else:
            callback_secret = self.config_data.get(
                'callback_secret',
                self.name + '-callback'
            )
            try:
                secret = kube_api.read_namespaced_secret(
                    callback_secret,
                    namespace
                )
                return base64.b64decode(secret.data['key'])
            except kubernetes.client.rest.ApiException as e:
                if e.status != 404:
                    raise
            return_key = random_string(16)
            kube_api.create_namespaced_secret(
                namespace,
                kubernetes.client.V1Secret(
                    metadata = kubernetes.client.V1ObjectMeta(
                        name = callback_secret
                    ),
                    data = {
                        'key': base64.b64encode(return_key)
                    }
                )
            )
            return return_key

    def webhook_key(self):
        '''Get webhook key value from config or secret'''
        if 'webhook_key' in self.config_data:
            return self.config_data['webhook_key']
        else:
            webhook_secret = self.config_data.get(
                'webhook_secret',
                self.name + '-webhook'
            )
            try:
                secret = kube_api.read_namespaced_secret(
                    webhook_secret,
                    namespace
                )
                return base64.b64decode(secret.data['key'])
            except kubernetes.client.rest.ApiException as e:
                if e.status != 404:
                    raise
            return None

    def ansible_vars(self):
        return self.config_data.get('vars', '{}')

    def git_url(self):
        return self.config_data.get('git_url', '')

    def git_ref(self):
        return self.config_data.get('git_ref', 'master')

    def start_run(self):
        if self.pod_running:
            raise Exception(
                '{} in start_run when pod {} is thought to be running'.format(
                    self.name,
                    self.pod_name
                )
            )
        if self.pod_name:
            logger.debug("Removing previous runner {} for {}".format(
                self.name,
                self.pod_name
            ))
            delete_runner_pod(self.pod_name)

        self._lock()
        try:
            self.pod_name = None
            self.last_run_time = time.time()
            self._start_runner_pod()
            self.update_status_config_map()
        finally:
            self._release()

    def _start_runner_pod(self):
        check_mode = self.check_mode()
        pod_name = 'ansible-runner-{}-{}'.format(
            self.name,
            random_string(5)
        )
        logger.debug("Starting runner {} for {}".format(
            pod_name,
            self.name
        ))
        kube_api.create_namespaced_pod(
            namespace,
            kubernetes.client.V1Pod(
                metadata = kubernetes.client.V1ObjectMeta(
                    name = pod_name,
                    labels = {
                        'openshift-provision.gnuthought.com/configmap': self.name,
                        'openshift-provision.gnuthought.com/runner': 'true'
                    }
                ),
                spec = kubernetes.client.V1PodSpec(
                    containers = [
                        kubernetes.client.V1Container(
                            name = 'runner',
                            env = [
                                kubernetes.client.V1EnvVar(
                                    name = 'ANSIBLE_VARS',
                                    value = self.ansible_vars()
                                ),
                                kubernetes.client.V1EnvVar(
                                    name = 'CALLBACK_URL',
                                    value = self.callback_url()
                                ),
                                kubernetes.client.V1EnvVar(
                                    name = 'CHECK_MODE',
                                    value = 'true' if check_mode else 'false'
                                ),
                                kubernetes.client.V1EnvVar(
                                    name = 'CONFIG_PATH',
                                    value = self.config_data.get('config_path', '')
                                ),
                                kubernetes.client.V1EnvVar(
                                    name = 'GIT_REF',
                                    value = self.git_ref()
                                ),
                                kubernetes.client.V1EnvVar(
                                    name = 'GIT_URL',
                                    value = self.git_url()
                                )
                            ],
                            image = runner_image,
                            image_pull_policy = "Always",
                            volume_mounts = [
                                kubernetes.client.V1VolumeMount(
                                    mount_path = '/opt/openshift-provision/run',
                                    name = 'rundir'
                                )
                            ]
                        )
                    ],
                    restart_policy = 'Never',
                    service_account_name = self.service_account(),
                    volumes = [
                        kubernetes.client.V1Volume(
                            name = 'rundir',
                            empty_dir = kubernetes.client.V1EmptyDirVolumeSource()
                        )
                    ]
                )
            )
        )
        logger.debug("Runner {} started for {}".format(
            pod_name,
            self.name
        ))
        self.pod_name = pod_name
        self.pod_running = True

    def check_lost_runner_pod(self):
        self._lock()
        try:
            if not self.pod_running \
            or self.last_run_time > time.time() - 30:
                return
            self._check_lost_runner_pod()
        finally:
            self._release()

    def _check_lost_runner_pod(self):
        try:
            pod = kube_api.read_namespaced_pod(
                self.pod_name,
                namespace
            )
            logger.debug("Runner pod {} for {} is {}".format(
                self.pod_name,
                self.name,
                pod.status.phase
            ))
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                logger.warn("Provision config {} lost runner pod {}!".format(
                    self.name,
                    self.pod_name
                ))
                self.handle_run_lost()
            else:
                raise

    def handle_runner_pod(self, pod):
        self._lock()
        try:
            self._handle_runner_pod(pod)
        finally:
            self._release()

    def _handle_runner_pod(self, pod):
        if pod.metadata.deletion_timestamp:
            logger.debug("Ignoring pod {} with deletion timestamp".format(
                pod.metadata.name
            ))
        elif self.pod_name == None:
            logger.warn("Recovered pod {} for {}".format(
                pod.status.phase,
                pod.metadata.name,
                self.name
            ))
            self.pod_name = pod.metadata.name
            if pod.status.phase in ('Pending', 'Running'):
                self.pod_running = True
        elif pod.metadata.name != self.pod_name:
            logger.warn("Found unexpected {} pod {} for {}, deleting it".format(
                pod.status.phase,
                pod.metadata.name,
                self.name
            ))
            delete_runner_pod(pod.metadata.name)
        elif pod.status.phase in ('Failed', 'Unknown'):
            logger.warn("Pod {} for {} found in {} state".format(
                pod.metadata.name,
                self.name,
                pod.status.phase
            ))
            self.handle_run_failure()
        elif pod.status.phase == 'Succeeded':
            self.pod_running = False
            logger.info("Pod {} for {} suceeeded".format(
                pod.metadata.name,
                self.name,
                pod.status.phase
            ))
        else:
            logger.debug("Handler observed runner {} for {} is {}".format(
                pod.metadata.name,
                self.name,
                pod.status.phase
            ))

    def handle_run_result(self, change_yaml):
        self.record_run_result(change_yaml)
        self.queue_next_run()

    def record_run_result(self, change_yaml):
        self.change_yaml = change_yaml
        if '---' in change_yaml:
            # Changed if there are change records in the yaml document
            self.state = 'changed'
        else:
            self.state = 'provisioned'
        stat_config_state.labels(self.name).state(self.state)
        self.update_status_config_map()

    def handle_run_failure(self):
        self.record_run_failure()
        self.queue_next_run()

    def record_run_failure(self):
        self.change_yaml = "# FAILED"
        self.pod_running = False
        self.state = 'failed'
        self.update_status_config_map()

    def handle_run_lost(self):
        self.record_run_lost()
        self.queue_next_run()

    def record_run_lost(self):
        self.change_yaml = "# UNKNOWN"
        self.pod_running = False
        self.state = 'unknown'
        self.update_status_config_map()

    def get_status_config_map(self):
        status_config_map = None
        try:
            status_config_map = kube_api.read_namespaced_config_map(
                self.name + '-status',
                namespace
            ).data
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise
        return status_config_map

    def update_status_config_map(self):
        status_data = {
            'changes': self.change_yaml,
            'pod_running': 'true' if self.pod_running else 'false',
            'retry_interval': self.retry_interval(),
            'run_interval': self.run_interval(),
            'state': self.state
        }
        if self.pod_name:
            status_data['pod_name'] = self.pod_name

        if self.get_status_config_map():
            kube_api.patch_namespaced_config_map(
                self.name + '-status',
                namespace,
                { "data": status_data }
            )
        else:
            kube_api.create_namespaced_config_map(
                namespace,
                kubernetes.client.V1ConfigMap(
                    metadata = kubernetes.client.V1ObjectMeta(
                        name = self.name + '-status',
                        labels = {
                            'openshift-provision.gnuthought.com/status': 'true'
                        }
                    ),
                    data = status_data
                )
            )

    def queue_next_run(self):
        if self.state in ['failed', 'unknown']:
            config_queue.push(self.name, self.retry_interval())
        else:
            config_queue.push(self.name, self.run_interval())

class ConfigQueue:
    """
    A class to safely manage the config queue. The desired queue behavior has
    features of both a set and a fifo queue such that duplication is prevented
    while also implementing first-in, first-out behavior.
    """
    def __init__(self):
        self.queue = []
        self.delay_queue = {}
        self.lock = threading.RLock()
    def _process_delay_queue(self):
        for name, delay_until in self.delay_queue.items():
            if delay_until <= time.time():
                logger.debug("Moving {} from delay queue".format(name))
                self.queue.append(name)
                del self.delay_queue[name]
    def _insert_into_delay_queue(self, name, delay):
        """Add to delay queue if not present in immediate queue"""
        if name in self.queue or name in self.delay_queue:
            logger.debug("Not delay queueing {}, already queued".format(name))
            return
        logger.debug("Inserting {} into delay queue".format(name))
        self.delay_queue[name] = time.time() + time_to_sec(delay)
    def _insert_into_queue(self, name):
        """Add to immediate queue if not present"""
        self._remove_from_delay_queue(name)
        if name in self.queue:
            logger.debug("Not queueing {}, already queued".format(name))
        else:
            logger.debug("Inserting {} into queue".format(name))
            self.queue.insert(0, name)
    def _remove_from_delay_queue(self, name):
        """Remove config from delay queue if present"""
        if name in self.delay_queue:
            del self.delay_queue[name]
    def _remove_from_queue(self, name):
        """Remove config from queue if present"""
        self.queue = [n for n in self.queue if n != name]
    def push(self, name, delay=None):
        self.lock.acquire()
        self._process_delay_queue()
        if delay:
            self._insert_into_delay_queue(name, delay)
        else:
            self._insert_into_queue(name)
        self.lock.release()
        signal_provision_start()
    def pop(self):
        name = None
        self.lock.acquire()
        self._process_delay_queue()
        name = None
        for i in range(len(self.queue)):
             config = provision_configs[self.queue[i]]
             if config.pod_running:
                 logger.debug("Queue not releasing {}, still running".format(config.name))
             else:
                 name = self.queue.pop(i)
                 logger.debug("Queue releasing {}".format(name))
                 break
        self.lock.release()
        return name
    def remove(self, name):
        """Remove config name from queue"""
        self.lock.acquire()
        self._remove_from_delay_queue(name)
        self._remove_from_queue(name)
        self.lock.release()

def init():
    """Initialization function before management loops."""
    init_logging()
    init_namespace()
    init_service_account_token()
    init_kube_api()
    init_provision_configs()
    init_runner_pods()
    init_queueing()
    logger.debug("Completed init")

def init_kube_api():
    """Set kube_api global to communicate with the local kubernetes cluster."""
    global kube_api
    kube_config = kubernetes.client.Configuration()
    kube_config.api_key['authorization'] = service_account_token
    kube_config.api_key_prefix['authorization'] = 'Bearer'
    kube_config.host = os.environ['KUBERNETES_PORT'].replace('tcp://', 'https://', 1)
    kube_config.ssl_ca_cert = '/run/secrets/kubernetes.io/serviceaccount/ca.crt'
    kube_api = kubernetes.client.CoreV1Api(
        kubernetes.client.ApiClient(kube_config)
    )

def init_logging():
    """Define logger global and set default logging level.
    Default logging level is INFO and may be overridden with the
    LOGGING_LEVEL environment variable.
    """
    global logger
    logging.basicConfig(
        format='%(levelname)s %(threadName)s - %(message)s',
    )
    logger = logging.getLogger('manager')
    logger.setLevel(os.environ.get('LOGGING_LEVEL', 'INFO'))

def init_namespace():
    """
    Set the namespace global based on the namespace in which this pod is
    running.
    """
    global namespace
    with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as f:
        namespace = f.read()

def init_queueing():
    global config_queue
    config_queue = ConfigQueue()
    provision_start_release.acquire()

def init_service_account_token():
    global service_account_token
    with open('/run/secrets/kubernetes.io/serviceaccount/token') as f:
        service_account_token = f.read()

def init_provision_configs():
    '''
    Get list of configmaps and set provision_configs without triggering
    processing.
    '''
    for config_map in kube_api.list_namespaced_config_map(
        namespace,
        label_selector = "openshift-provision.gnuthought.com/config=true"
    ).items:
        add_config(config_map.metadata.name)

def init_runner_pods():
    '''
    Cleanup any old stopped pods and add record of any running pods.
    '''
    for pod in kube_api.list_namespaced_pod(
        namespace,
        label_selector = "openshift-provision.gnuthought.com/runner=true"
    ).items:
        handle_runner_pod(pod)

def handle_runner_pod(pod):
    config_name = pod.metadata.labels.get(
        'openshift-provision.gnuthought.com/configmap',
        None
    )
    if config_name == None:
        logger.warn("Ignoring runner pod {} without configmap label".format(
            pod.metadata.name
        ))
    elif config_name not in provision_configs:
        logger.warn("Deleting runner pod {} with unknown configmap: {}".format(
            pod.metadata.name,
            config_name
        ))
        delete_runner_pod(pod.metadata.name)
    else:
        provision_configs[config_name].handle_runner_pod(pod)

def signal_provision_start():
    """
    Signal provision loop that there is work to be done

    This is done by releasing the provision_start_release lock. This
    lock may be already released.
    """
    logger.debug("Signaling provision start")
    try:
        provision_start_release.release()
    except threading.ThreadError:
        pass

def provision_config(config):
    """
    Run openshift-provision for config
    """
    try:
        config.provision()
    except ProvisionConfigRemovedError:
        # ConfigMap was deleted, remove from processing
        logger.info("Removing deleted provisioning config {}".format(
            config.name
        ))
        del provision_configs[config.name]
    except ProvisionConfigInvalidError as e:
        logger.info("Removing invalid provisioning config {}: {}".format(
            config.name,
            str(e)
        ))
        del provision_configs[config.name]
    except Exception as e:
        logger.exception("Error in provision {}: {}".format(
            config.name,
            str(e)
        ))
        config_queue.push(config.name, delay=config.retry_interval())
        time.sleep(60)

def provision_config_queue():
    """
    Process all pending provisioning on config queue
    """
    logger.debug("Processing queue")
    config_name = config_queue.pop()
    while config_name:
        logger.info("Provisioning " + config_name)
        config = provision_configs.get(config_name, None)
        if config:
            provision_config(config)
        config_name = config_queue.pop()

def handle_lost_runner_pods():
    '''
    Loop through provision configs and check for pods that have been
    lost though the current state shows they should be running.

    This handles the case of a runner pod being manually deleted.
    '''
    for provision_config in provision_configs.values():
        provision_config.check_lost_runner_pod()

def provision_loop():
    while True:
        try:
            provision_start_release.acquire()
            provision_config_queue()
            handle_lost_runner_pods()
        except Exception as e:
            logger.exception("Error in provision_loop " + str(e))
            time.sleep(60)

def provision_trigger_loop():
    """
    Periodically release the provision_start_release lock to trigger
    scheduled provisioning.
    """
    while True:
        try:
            logger.debug("Provision start release trigger")
            signal_provision_start()
            time.sleep(10)
        except Exception as e:
            logger.exception("Error in provision_trigger_loop " + str(e))
            time.sleep(60)

def add_config(name):
    if name not in provision_configs:
        provision_configs[name] = ProvisionConfig(name)
        stat_config_state.labels(name).state('unknown')

def remove_config(name):
    config_queue.remove(name)
    if name in provision_configs:
        stat_config_state.labels(name).state('removed')
        del provision_configs[name]

def watch_config_maps():
    w = kubernetes.watch.Watch()
    for event in w.stream(
        kube_api.list_namespaced_config_map,
        namespace,
        label_selector = "openshift-provision.gnuthought.com/config=true"
    ):
        config_map = event['object']
        logger.debug("Watch saw {} configmap {}".format(
            event['type'],
            config_map.metadata.name
        ))
        if event['type'] in ('ADDED','MODIFIED'):
            if config_map.metadata.deletion_timestamp:
                remove_config(config_map.metadata.name)
            else:
                add_config(config_map.metadata.name)
                config_queue.push(config_map.metadata.name)

def watch_config_maps_loop():
    while True:
        try:
            logger.debug("Starting watch for config maps")
            watch_config_maps()
        except Exception as e:
            logger.exception("Error in watch_config_maps " + str(e))
            time.sleep(60)

def delete_runner_pod(pod_name):
    logger.debug('Deleting pod ' + pod_name)
    try:
        kube_api.delete_namespaced_pod(
            pod_name,
            namespace,
            {}
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise

def watch_pods():
    logger.debug('Starting watch for runner pods')
    w = kubernetes.watch.Watch()
    for event in w.stream(
        kube_api.list_namespaced_pod,
        namespace,
        label_selector = 'openshift-provision.gnuthought.com/runner=true'
    ):
        pod = event['object']
        logger.debug("Watch saw {} pod {}".format(
            event['type'],
            pod.metadata.name
        ))
        if event['type'] in ('ADDED','MODIFIED'):
            handle_runner_pod(pod)

def pod_management_loop():
    while True:
        try:
            logger.debug("Starting watch for pods")
            watch_pods()
        except Exception as e:
            logger.exception("Error in watch_pods " + str(e))
            time.sleep(60)

@api.route('/provision/<string:name>/<string:key>', methods=['POST'])
def api_provision(name, key):
    logger.info("Webhook invoked for {}".format(name))
    if name not in provision_configs:
        logger.warn("Webhook {} not found".format(name))
        flask.abort(404)
        return
    config = provision_configs[name]
    if key != config.webhook_key():
        logger.warn("Invalid webhook key for {}".format(name))
        flask.abort(403)
        return
    stat_webhook_call_count.labels(name).inc()
    config.set_next_time_no_check_mode()
    config_queue.push(name)
    return flask.jsonify({'status': 'queued'})

@api.route('/check/<string:name>/<string:key>', methods=['POST'])
def api_check(name, key):
    logger.info("Webhook invoked for {}".format(name))
    if name not in provision_configs:
        logger.warn("Webhook {} not found".format(name))
        flask.abort(404)
        return
    config = provision_configs[name]
    if key != config.webhook_key():
        logger.warn("Invalid webhook key for {}".format(name))
        flask.abort(403)
        return
    stat_webhook_call_count.labels(name).inc()
    config_queue.push(name)
    return flask.jsonify({'status': 'queued'})

@api.route('/callback/<string:name>/<string:key>', methods=['POST'])
def api_callback(name, key):
    logger.info("Callback invoked for {}".format(name))
    if name not in provision_configs:
        logger.warn("Callback {} not found".format(name))
        flask.abort(404)
        return
    config = provision_configs[name]
    if key != config.callback_key():
        logger.warn("Invalid callback key for {}".format(name))
        flask.abort(403)
        return
    provision_configs[name].handle_run_result(flask.request.data)
    return flask.jsonify({'status': 'ok'})

def main():
    """Main function."""
    init()
    threading.Thread(
        name = 'Provision',
        target = provision_loop
    ).start()
    threading.Thread(
        name = 'Watch',
        target = watch_config_maps_loop
    ).start()
    threading.Thread(
        name = 'Trigger',
        target = provision_trigger_loop
    ).start()
    threading.Thread(
        name = 'PodManager',
        target = pod_management_loop
    ).start()
    prometheus_client.start_http_server(8000)
    http_server  = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
