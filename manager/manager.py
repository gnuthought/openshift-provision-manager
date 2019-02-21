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
import yaml

api = flask.Flask('rest')

stat_config_state = prometheus_client.Enum(
    'config_state',
    'Configuration state',
    ['name'],
    states=[
        'unknown',
        'provisioned',
        'changed',
        'check-failed',
        'removed',
        'invalid'
    ]
)
stat_run_state = prometheus_client.Enum(
    'runner_state',
    'Runner state',
    ['name'],
    states=[
        'new',
        'config-updated',
        'running',
        'received-callback',
        'succeeded',
        'failed',
        'triggered',
        'disabled'
    ]
)

runnable_states = ('new','config-updated','succeeded','failed','triggered')

pod_ip = socket.gethostbyname(os.environ['HOSTNAME'])

# Variables initialized during init()
namespace = None
kube_api = None
logger = None
service_account_token = None

# Delay from initial config appears to first run. Allows for time for service
# account token creation for runner pod.
default_initial_delay = os.environ.get('INITIAL_DELAY', '5s')

# Delay for next run after change or check-failed
default_recheck_interval = os.environ.get('RECHECK_INTERVAL', '5m')

# Delay for retry after a failed run
default_retry_interval = os.environ.get('RETRY_INTERVAL', '10m')

# Delay for next run after succesful run
default_run_interval = os.environ.get('RUN_INTERVAL', '30m')

# Delay for next run after succesful run
default_run_timeout = os.environ.get('RUN_TIMEOUT', '30m')

# Global list of known provision configurations
provision_configs = {}

# Interval to check configurations for which are ready to run
poll_interval = os.environ.get('POLL_INTERVAL', '10s')

# Lock used by webhook to trigger immediate run
polling_release_lock = threading.Lock()

# Container image to use for openshift-ansible runner
runner_image = os.environ.get(
    'RUNNER_IMAGE',
    'docker.io/gnuthought/openshift-provision-runner:latest'
)

class ProvisionConfigInvalidError(Exception):
    pass

class ProvisionConfigRemovedError(Exception):
    pass

def is_truthy(s):
    return s in ('yes', 'Yes', 'true', 'True')

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

def polling_release():
    """
    Signal provision loop that there is work to be done

    This is done by releasing the provision_start_release lock. This
    lock may be already released.
    """
    try:
        polling_release_lock.release()
    except threading.ThreadError:
        pass

def delete_pod(pod_name):
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

class ProvisionConfig:
    def __init__(self, config_map):
        self.lock = threading.RLock()
        self.callback_key = random_string(16)
        self.config_data = config_map.data
        self.change_yaml = None
        self.last_start_time = 0
        self.last_end_time = 0
        self.name = config_map.metadata.name
        self.next_run_no_check_mode = False
        self.next_config_state = None
        self.next_run_state = None
        self.pod_name = None
        self.previous_pod_name = None
        self.config_state = None
        self.run_state = 'new'
        self.running_check_mode = False

        self._lock()
        try:
            self._set_config_state('unknown')
            self._set_run_state('new')
        finally:
            self._release()

    def _lock(self):
        self.lock.acquire()

    def _release(self):
        self.lock.release()

    def initial_delay(self):
        return time_to_sec(
            self.config_data.get('initial_delay', default_initial_delay)
        )

    def _set_config_state(self, state):
        stat_config_state.labels(self.name).state(state)
        self.config_state = state
        if state in ('unknown','config-removed','config-invalid'):
            self.change_yaml = '# ' + state
        self.update_status_config_map()

    def _set_run_state(self, state):
        stat_run_state.labels(self.name).state(state)
        self.run_state = state
        if self.is_runnable():
            self._set_next_start_time()

    def _set_state_transition(self, run_state=None, config_state=None):
        '''Set next state or immediate state as appropriate'''
        if self.run_state in ('running', 'received-callback'):
            self.next_config_state = config_state
            self.next_run_state = run_state
        else:
            self._set_config_state(config_state)
            self._set_run_state(run_state)

    def _set_next_start_time(self):
        if self.run_state in ('triggered'):
            delay = 0
        elif self.run_state in ('new', 'config-updated'):
            delay = self.initial_delay()
        elif self.run_state in ('succeeded'):
            if self.config_state in ('provisioned'):
                delay = self.run_delay()
            else:
                delay = self.recheck_delay()
        else:
            delay = self.retry_delay()
        self.next_start_time = time.time() + delay

    def poll(self):
        self.check_start_run()
        self.check_run_timeout()

    def is_runnable(self):
        return self.run_state in runnable_states

    def recheck_delay(self):
        return time_to_sec(
            self.config_data.get('recheck_interval', default_recheck_interval)
        )

    def retry_delay(self):
        return time_to_sec(
            self.config_data.get('retry_interval', default_retry_interval)
        )

    def run_delay(self):
        return time_to_sec(
            self.config_data.get('run_interval', default_run_interval)
        )

    def run_timeout(self):
        return time_to_sec(
            self.config_data.get('run_timeout', default_run_timeout)
        )

    def check_mode(self):
        if self.next_run_no_check_mode:
            self.next_run_no_check_mode = False
            return False
        return is_truthy( self.config_data.get('check_mode','no') )

    def service_account(self):
        return self.config_data.get('service_account', self.name)

    def callback_url(self):
        '''Return URL for runner pod callback'''
        return 'http://{}:5000/callback/{}/{}'.format(
            pod_ip,
            self.name,
            self.callback_key
        )

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

    def webhook_triggered(self, no_check_mode=False):
        self._lock()
        try:
            if no_check_mode:
                self.next_run_no_check_mode = True
            self._set_state_transition(run_state='triggered')
        finally:
            self._release()

    def ansible_vars(self):
        return self.config_data.get('vars', '{}')

    def git_url(self):
        return self.config_data.get('git_url', '')

    def git_ref(self):
        return self.config_data.get('git_ref', 'master')

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
            'change_record': self.change_yaml,
            'config_state': self.config_state
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

    def _delete_previous_runner_pod(self):
        '''Delete runner pod if it exists'''
        if self.previous_pod_name:
            logger.debug('Deleting previous runner pod {} for {}'.format(
                self.previous_pod_name, self.name
            ))
            delete_pod(self.previous_pod_name)
            self.previous_pod_name = None

    def check_start_run(self):
        self._lock()
        try:
            if self.is_runnable() \
            and time.time() >= self.next_start_time:
                self.previous_pod_name = self.pod_name
                self._start_run()
        except Exception as e:
            logger.error("Failed to start run for {}".format(self.name))
            self._set_run_state('failed')
            raise
        finally:
            self._release()

    def _start_run(self):
        self.pod_name = None
        self._set_run_state('running')
        self._delete_previous_runner_pod()
        self._start_runner_pod()
        self.update_status_config_map()

    def _start_runner_pod(self):
        self.running_check_mode = self.check_mode()
        self.last_start_time = time.time()
        self.pod_name = 'ansible-runner-{}-{}'.format(self.name, random_string(5))
        logger.debug("Starting runner {} for {}".format(self.pod_name, self.name))
        kube_api.create_namespaced_pod(
            namespace,
            kubernetes.client.V1Pod(
                metadata = kubernetes.client.V1ObjectMeta(
                    name = self.pod_name,
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
                                    value = 'true' if self.running_check_mode else 'false'
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

    def handle_runner_pod_event(self, event_type, pod):
        self._lock()
        try:
            self._handle_runner_pod_event(event_type, pod)
        finally:
            self._release()

    def _handle_runner_pod_event(self, event_type, pod):
        if self.pod_name != pod.metadata.name:
            return
        elif event_type == 'DELETED':
            self._handle_runner_deleted(pod)
        elif event_type in ('ADDED','MODIFIED') \
        and not pod.metadata.deletion_timestamp:
            if pod.status.phase == 'Succeeded':
                self._handle_runner_succeeded()
            elif pod.status.phase == 'Failed':
                self._handle_runner_failed()
            else:
                logger.debug("Runner pod {} for {} is {}".format(
                    self.pod_name, self.name, pod.status.phase
                ))

    def _handle_runner_deleted(self, pod):
        self.pod_name = None
        logger.error("Runner pod {} for {} deleted unexpectedly from run state {}".format(
            pod.metadata.name, self.name, self.run_state
        ))
        if self.run_state == 'running':
            self._transition_to_next_state(run_state='failed', config_state='unknown')

    def _handle_runner_succeeded(self):
        if self.run_state == 'succeeded':
            return
        elif self.run_state != 'received-callback':
            logger.error("Runner for {} succeeded from state {} != 'received-callback'?!".format(
                self.name, self.run_state
            ))
        self._transition_to_next_state(run_state='succeeded')

    def _handle_runner_failed(self):
        if self.run_state == 'failed':
            return
        if self.run_state != 'running':
            logger.error("Runner for {} succeeded from state {} != 'running'?!".format(
                self.name, self.run_state
            ))
        self._transition_to_next_state(run_state='failed', config_state='unknown')

    def _transition_to_next_state(self, run_state=None, config_state=None):
        '''
        Transition to next state or to state specified.

        Called when runner pod completes with succes or failure to transition
        to the next state, either handling the run result or moving to state
        set by config change or trigger.
        '''
        if self.next_run_state:
            self._set_run_state(next_run_state)
            self.next_run_state = None
        elif run_state:
            self._set_run_state(run_state)

        if self.next_config_state:
            self._set_config_state(self.next_config_state)
            self.next_config_state = None
        elif config_state:
            self._set_config_state(config_state)

    def handle_config_map_update(self, config_map):
        self._lock()
        try:
            set_config_state_value = 'unknown'
            set_run_state_value = 'config-updated'
            self.config_data = config_map.data
            self.sanity_check()
        except ProvisionConfigInvalidError as e:
            logger.error("Config {} failed sanity check: {}".format(
                self.name, e
            ))
            set_config_state_value = 'invalid'
            set_run_state_value = 'disabled'
        finally:
            try:
                self._set_state_transition(
                    run_state=set_run_state_value,
                    config_state=set_config_state_value
                )
            finally:
                self._release()

    def handle_config_map_removed(self):
        self._lock()
        try:
            self._set_state_transition(self, run_state='removed', config_state='disabled')
        finally:
            self._release()

    def sanity_check(self):
        for time_field in (
            'initial_delay','recheck_interval','retry_interval','run_interval','run_timeout'
        ):
            if time_field in self.config_data \
            and not re.match(r'\d+[hms]?$', self.config_data[time_field]):
                raise ProvisionConfigInvalidError('Invalid time value for {}: {}'.format(
                    time_field, e
                ))

            if 'check_mode' in self.config_data \
            and self.config_data['check_mode'] not in (
                'Yes', 'yes', 'true', 'True',
                'No', 'no', 'false', 'False'
            ):
                raise ProvisionConfigInvalidError('Invalid value for check_mode: {}'.format(
                    self.config_data['check_mode']
                ))

            if 'vars' in self.config_data:
                try:
                    yaml.load(self.config_data['vars'])
                except yaml.parser.ParserError as e:
                    raise ProvisionConfigInvalidError('Invalid value for vars: {}'.format(e))

    def check_run_timeout(self):
        self._lock()
        try:
            if self.run_state in ('running', 'received-callback') \
            and time.time() > self.last_start_time + self.run_timeout():
                logger.debug('Timeout waiting on runner pod {} for {}'.format(
                    self.pod_name, self.name
                ))
                self.previous_pod_name = self.pod_name
                self.pod_name = None
                self._delete_previous_runner_pod()
                self._transition_to_next_state(run_state='failed', config_state='unknown')
        finally:
            self._release()

    def handle_callback_data(self, change_yaml):
        self._lock()
        try:
            if self.run_state == 'running':
                self.change_yaml = change_yaml
                if '---' in change_yaml:
                    # Changed if there are change records in the yaml document
                    self._set_config_state('check-failed' if self.running_check_mode else 'changed')
                else:
                    self._set_config_state('provisioned')
                self._set_run_state('received-callback')
            else:
                logger.error("Received callback for {} while in state {}".format(
                    self.name, self.run_state
                ))
        finally:
            self._release()

def init():
    """Initialization function before management loops."""
    init_logging()
    init_namespace()
    init_service_account_token()
    init_kube_api()
    init_cleanup_runner_pods()
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

def init_service_account_token():
    global service_account_token
    with open('/run/secrets/kubernetes.io/serviceaccount/token') as f:
        service_account_token = f.read()

def init_cleanup_runner_pods():
    '''
    Cleanup any old stopped pods and add record of any running pods.
    '''
    for pod in kube_api.list_namespaced_pod(
        namespace,
        label_selector = "openshift-provision.gnuthought.com/runner=true"
    ).items:
        delete_pod(pod.metadata.name)

def poll_provision_configs():
    '''Call poll on each provision config'''
    for config in provision_configs.values():
        config.poll()

def poll_provision_loop():
    while True:
        try:
            polling_release_lock.acquire()
            poll_provision_configs()
        except Exception as e:
            logger.exception("Error in poll_provision_loop " + str(e))
            time.sleep(60)

def poll_trigger_loop():
    """
    Periodically release the provision_start_release lock to trigger
    scheduled provisioning.
    """
    poll_interval_s = time_to_sec(poll_interval)
    while True:
        try:
            logger.debug("Provision start release trigger")
            polling_release()
            time.sleep(poll_interval_s)
        except Exception as e:
            logger.exception("Error in poll_trigger_loop " + str(e))
            time.sleep(60)

def set_config(config_map):
    name = config_map.metadata.name
    config = provision_configs.get(name, None)
    if config:
        config.handle_config_map_update(config_map)
    else:
        provision_configs[name] = ProvisionConfig(config_map)

def remove_config(config_map):
    config = provision_configs.get(config_map.metadata.name, None)
    if config:
        config.handle_config_map_removed()

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
        if event['type'] in ('ADDED','MODIFIED') \
        and not config_map.metadata.deletion_timestamp:
            set_config(config_map)
        elif event_type == 'DELETED':
            remove_config(config_map)

def watch_config_maps_loop():
    while True:
        try:
            logger.debug("Starting watch for config maps")
            watch_config_maps()
        except Exception as e:
            logger.exception("Error in watch_config_maps " + str(e))
            time.sleep(60)

def watch_pods():
    logger.debug('Starting watch for runner pods')
    w = kubernetes.watch.Watch()
    for event in w.stream(
        kube_api.list_namespaced_pod,
        namespace,
        label_selector = 'openshift-provision.gnuthought.com/runner=true'
    ):
        pod = event['object']
        config_name = pod.metadata.labels.get(
            'openshift-provision.gnuthought.com/configmap',
            None
        )
        if config_name == None:
            logger.warn("Deleting runner pod {} without configmap label".format(
                pod.metadata.name
            ))
            delete_pod(pod.metadata.name)
        elif config_name not in provision_configs:
            logger.warn("Deleting runner pod {} with unknown configmap: {}".format(
                pod.metadata.name,
                config_name
            ))
            delete_pod(pod.metadata.name)
        else:
            provision_configs[config_name].handle_runner_pod_event(event['type'], pod)

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
    config.webhook_triggered(True)
    polling_release()
    return flask.jsonify({'status': 'ok'})

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
    config.webhook_triggered(False)
    polling_release()
    return flask.jsonify({'status': 'ok'})

@api.route('/callback/<string:name>/<string:key>', methods=['POST'])
def api_callback(name, key):
    logger.info("Callback invoked for {}".format(name))
    if name not in provision_configs:
        logger.warn("Callback {} not found".format(name))
        flask.abort(404)
        return
    config = provision_configs[name]
    if key != config.callback_key:
        logger.warn("Invalid callback key for {}".format(name))
        flask.abort(403)
        return
    provision_configs[name].handle_callback_data(flask.request.data)
    return flask.jsonify({'status': 'ok'})

def main():
    """Main function."""
    init()
    threading.Thread(
        name = 'Provision',
        target = poll_provision_loop
    ).start()
    threading.Thread(
        name = 'Trigger',
        target = poll_trigger_loop
    ).start()
    threading.Thread(
        name = 'WatchConfig',
        target = watch_config_maps_loop
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
