#!/usr/bin/env python

import base64
import datetime
import flask
import gevent.pywsgi
import kubernetes
import kubernetes.client.rest
import logging
import os
import prometheus_client
import re
import shutil
import subprocess
import sys
import threading
import time
import yaml

api = flask.Flask('rest')

stat_config_state = prometheus_client.Enum(
    'config_state',
    'Configuration state',
    ['name'],
    states=['new','provisioned','changed','failed','removed']
)
stat_webhook_call_count = prometheus_client.Counter(
    'webhook_call_count',
    'Number of webhook calls',
    ['name']
)

config_queue = None
namespace = None
provision_configs = {}
git_base_path = os.environ.get('GIT_CHECKOUT_DIR', '/opt/openshift-provision/cache/git')
ansible_runner_base_path = os.environ.get('ANSIBLE_RUNNER_DIR', '/opt/openshift-provision/cache/ansible-runner')
default_retry_interval = os.environ.get('RETRY_INTERVAL', '10m')
default_run_interval = os.environ.get('RUN_INTERVAL', '30m')
git_env = {
    "GIT_COMMITTER_NAME": "openshift-provision",
    "GIT_COMMITTER_EMAIL": "openshift-provision@gmail.com"
}
kube_api = None
logger = None
openshift_provision_playbook = os.environ.get('OPENSHIFT_PROVISION_PLAYBOOK', '/opt/openshift-provision/openshift-provision.yaml')
provision_config_lock = threading.RLock()
provision_start_release = threading.Lock()
service_account_token = None

class ProvisionConfigInvalidError(Exception):
    pass
class ProvisionConfigRemovedError(Exception):
    pass

def is_truthy(s):
    return s in ['yes', 'Yes', 'true', 'True']

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

def run_as_ansible():
    """If running as root, switch to ansible user before executing ansible"""
    if 0 == os.geteuid():
        os.setgid(1000)
        os.setuid(1000)

class ProvisionConfig:
    def __init__(self, namespace, name):
        self.config_data = {}
        self.git_path = '{}/{}'.format(git_base_path, name)
        self.last_run_return = None
        self.last_run_time = 0
        self.lock = threading.Lock()
        self.name = name
        self.namespace = namespace
        self.next_time_no_check_mode = False
        self.pod_name = None
        self.retry_interval = default_retry_interval
        self.run_interval = default_run_interval

    def provision(self):
        self.configmap_refresh()
        self.git_refresh()
        self.run_openshift_provision_ansible()

    def configmap_refresh(self):
        try:
            self.config_data = kube_api.read_namespaced_config_map(
                self.name,
                self.namespace
            ).data
            self.config_data_sanitycheck()
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                raise ProvisionConfigRemovedError
            else:
                raise

    def config_data_sanitycheck(self):
        for reqfield in ['git_uri']:
            if reqfield not in self.config_data:
                raise ProvisionConfigInvalidError(
                    'Missing required field ' + reqfield
                )

    def check_mode(self):
        if self.next_time_no_check_mode:
            self.next_time_no_check_mode = False
            return False
        return is_truthy(self.config_data.get('check_mode','no'))

    def set_next_time_no_check_mode(self):
        self.next_time_no_check_mode = True

    def service_account(self):
        if 'service_account' in self.config_data:
            return self.config_data['service_account']
        return self.name

    def webhook_key(self):
        if 'webhook_secret' in self.config_data:
            secret = kube_api.read_namespaced_secret(
                self.config_data['webhook_secret'],
                namespace
            )
            return base64.b64decode(secret.data['key'])
        elif 'webhook_key' in self.config_data:
            return self.config_data['webhook_key']
        return None

    def service_account_token(self):
        service_account_name = self.service_account()
        service_account = kube_api.read_namespaced_service_account(
            service_account_name,
            namespace
        )
        for secret in service_account.secrets:
            if secret.name.startswith(service_account_name + '-token-'):
                secret = kube_api.read_namespaced_secret(
                    secret.name,
                    namespace
                )
                return base64.b64decode(secret.data['token'])

    def git_uri(self):
        return self.config_data['git_uri']

    def git_branch(self):
        return self.config_data.get('git_branch', 'master')

    def git_config_path(self):
        path = self.git_path
        if 'config_path' in self.config_data:
            path += '/' + self.config_data['config_path']
        return path

    def git_clone(self):
        logger.info("Performing git clone for " + self.name)
        subprocess.check_call(
            [
                'git', 'clone', self.git_uri(),
                '--single-branch', '--branch', self.git_branch(),
                self.git_path
            ],
            env = git_env,
            stderr = sys.stderr
        )

    def git_pull(self):
        logger.info("Pulling git updates for " + self.name)
        subprocess.check_call(
            [
                'git', 'fetch', '--force', 'origin'
            ],
            cwd = self.git_path,
            env = git_env,
            stderr = sys.stderr
        )
        subprocess.check_call(
            [
                'git', 'reset', '--hard', 'origin/' + self.git_branch()
            ],
            cwd = self.git_path,
            env = git_env,
            stderr = sys.stderr
        )

    def git_refresh(self):
        if os.path.isdir(self.git_path):
            self.git_pull()
        else:
            self.git_clone()

    def run_openshift_provision_ansible(self):
        self.last_run_time = time.time()
        check_mode = self.check_mode()
        ansible_dir = self.prepare_ansible_runner_dir()
        ansible_run = self.do_ansible_run(ansible_dir, check_mode)
        self.record_run_result(ansible_run, ansible_dir)
        self.queue_next_run(ansible_run)

    def prepare_ansible_runner_dir(self):
        """
        Prepare directory for ansible run. Originally this was designed for use
        with ansible-runner, but then it was fonud that ansible runner does not
        expose a change record.
        """
        logger.debug("Preparing ansible runner for " + self.name)
        private_data_dir = "{}/{}".format(ansible_runner_base_path, self.name)
        change_record = private_data_dir + "/change-record.yaml"
        if os.path.isdir(private_data_dir):
            shutil.rmtree(private_data_dir)
        for subdir in ('env', 'project'):
            os.makedirs(private_data_dir + '/' + subdir)

        # Initialize change record
        with open(change_record, "w") as fh:
            fh.write("# Ansible run for {} - {}Z\n".format(
                self.name,
                datetime.datetime.utcnow().isoformat()
            ))
        # Make change record writable for ansible
        os.chmod(change_record, 0o666)

        # Define extravars
        extravars = {
            'openshift_connection_certificate_authority': 
                ansible_runner_base_path + '/ca.crt',
            'openshift_connection_server': 
                'https://kubernetes.default.svc',
            'openshift_connection_token': 
                self.service_account_token(),
            'openshift_provision_change_record': 
                change_record,
            'openshift_provision_config_path': 
                self.git_config_path()
        }
        if 'params' in self.config_data:
            config_params = yaml.safe_load(self.config_data['params'])
            extravars.update(config_params)
        with open(private_data_dir + '/env/extravars', 'w') as fh:
            yaml.safe_dump(extravars, fh)

        shutil.copyfile(
            openshift_provision_playbook,
            private_data_dir + '/project/openshift-provision.yaml')
        if os.path.isdir(self.git_path + '/filter_plugins'):
            os.symlink(
                self.git_path + '/filter_plugins',
                private_data_dir + '/project/filter_plugins'
            )
        return private_data_dir

    def do_ansible_run(self, ansible_dir, check_mode):
        ansible_cmd = [
            "ansible-playbook",
            "--extra-vars=@{}/env/extravars".format(ansible_dir),
            "openshift-provision.yaml"
        ]
        if check_mode:
            ansible_cmd.append('--check')
        # FIXME --vault-password-file support?
        ansible_run = subprocess.Popen(
            ansible_cmd,
            cwd = ansible_dir + '/project',
            env = { "HOME": "/home/ansible" },
            preexec_fn = run_as_ansible,
            stderr = subprocess.STDOUT,
            stdout = subprocess.PIPE
        )
        while True:
            output = ansible_run.stdout.readline()
            if output == '' and ansible_run.poll() is not None:
                break
            output = re.sub(r' *\**\n$', '', output)
            if output:
                logger.info(output)
        logger.info("Return code {}".format(ansible_run.returncode))
        return ansible_run

    def record_run_result(self, ansible_run, ansible_dir):
        self.last_run_return = ansible_run.returncode
        change_record = ansible_dir + "/change-record.yaml"
        change_yaml = None
        if os.path.isfile(change_record):
            with open(change_record) as f:
               change_yaml = f.read()

        if ansible_run.returncode != 0:
            state = 'failed'
        elif change_yaml \
        and '---' in change_yaml:
            # Changed if there are change records in the yaml document
            state = 'changed'
        else:
            state = 'provisioned'

        stat_config_state.labels(self.name).state(state)
        self.update_status_config_map(
            change_yaml = change_yaml,
            state = state
        )

    def get_status_config_map(self):
        status_config_map = None
        try:
            status_config_map = kube_api.read_namespaced_config_map(
                self.name + '-status',
                self.namespace
            ).data
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise
        return status_config_map

    def update_status_config_map(self, change_yaml, state):
        status_config_map = self.get_status_config_map()
        status_data = {
            "changes": change_yaml,
            "state": state
        }
        if status_config_map:
            kube_api.patch_namespaced_config_map(
                self.name + '-status',
                self.namespace,
                { "data": status_data }
            )
        else:
            kube_api.create_namespaced_config_map(
                self.namespace,
                kubernetes.client.V1ConfigMap(
                    metadata = kubernetes.client.V1ObjectMeta(
                        name = self.name + '-status',
                        labels = {
                            "openshift-provision.gnuthought.com/status": "true"
                        }
                    ),
                    data = status_data
                )
            )

    def queue_next_run(self, ansible_run):
        if ansible_run.returncode == 0:
            config_queue.push(self.name, self.run_interval)
        else:
            config_queue.push(self.name, self.retry_interval)

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
        if self.queue:
            name = self.queue.pop()
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
    init_dirs()
    init_logging()
    init_namespace()
    init_queueing()
    init_service_account_token()
    init_kube_api()

def init_dirs():
    for path in [
        git_base_path,
        ansible_runner_base_path
    ]:
        if not os.path.isdir(path):
            os.makedirs(path)

    # Make cluster ca.crt available to ansible runs
    shutil.copyfile(
        '/run/secrets/kubernetes.io/serviceaccount/ca.crt',
        ansible_runner_base_path + '/ca.crt'
    )

    # Protect secrets from ansible lookups when running as root
    if 0 == os.geteuid():
        os.chmod("/run/secrets", 0o700)

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
        format='%(asctime)-15s %(levelname)s %(threadName)s - %(message)s',
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

def signal_provision_start():
    """
    Signal provision loop that there is work to be done

    This is done by releasing the provision_start_release lock. This lock may
    already released.
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
        config_queue.push(config.name, delay=config.retry_interval)
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

def provision_loop():
    while True:
        try:
            provision_start_release.acquire()
            provision_config_queue()
        except Exception as e:
            logger.exception("Error in provision_loop " + str(e))
            time.sleep(60)

def provision_trigger_loop():
    """
    Periodically release the provision_start_release lock to trigger scheduled
    provisioning.
    """
    while True:
        try:
            logger.debug("Provision start release trigger")
            signal_provision_start()
            time.sleep(10)
        except Exception as e:
            logger.exception("Error in provision_trigger_loop " + str(e))
            time.sleep(60)

def set_config(name):
    if name not in provision_configs:
        provision_configs[name] = ProvisionConfig(
            namespace,
            name
        )
        stat_config_state.labels(name).state('new')
    config_queue.push(name)

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
        if event['type'] in ['ADDED','MODIFIED']:
            if config_map.metadata.deletion_timestamp:
                remove_config(config_map.metadata.name)
            else:
                set_config(config_map.metadata.name)

def watch_config_maps_loop():
    while True:
        try:
            logger.debug("Starting watch for config maps")
            watch_config_maps()
        except Exception as e:
            logger.exception("Error in watch_config_maps " + str(e))
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

@api.route('/queue/<string:name>/<string:key>', methods=['POST'])
def api_queue(name, key):
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
    prometheus_client.start_http_server(8000)
    http_server  = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
