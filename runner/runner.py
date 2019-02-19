#!/usr/bin/env python

import datetime
import logging
import os
import re
import requests
import shutil
import subprocess
import sys
import yaml

run_dir = os.environ.get('RUN_DIR', '/opt/openshift-provision/run')
git_dir = run_dir + '/git'
nss_wrapper_passwd = run_dir + '/passwd'
logger = None

process_env = {
    'HOME': run_dir,
    'LD_PRELOAD': 'libnss_wrapper.so',
    'NSS_WRAPPER_PASSWD': nss_wrapper_passwd
}

openshift_provision_playbook = os.environ.get(
    'OPENSHIFT_PROVISION_PLAYBOOK',
    '/opt/openshift-provision/openshift-provision.yaml'
)

def die(msg):
    logger.error(msg)
    sys.exit(1)

def is_truthy(s):
    return s in ['yes', 'Yes', 'true', 'True']

def get_service_account_token():
    f = open('/run/secrets/kubernetes.io/serviceaccount/token')
    return f.read()

def report_changes():
    logger.debug("Sending change record to callback url")
    change_yaml = open(run_dir + '/change-record.yaml')
    r = requests.post(
        os.environ['CALLBACK_URL'],
        data = change_yaml,
        headers = {'content-type': 'text/plain'}
    )
    if r.status_code != 200:
        die("Received status code {} from callback url:\n{}".format(
           r.status_code,
           r.text
        ))
    logger.info("Sent change record to callback url")

def run_ansible():
    prepare_ansible_dir()
    do_ansible_run()

def prepare_ansible_dir():
    """
    Prepare directory for ansible run. Originally this was designed for use
    with ansible-runner, but then it was fonud that ansible runner does not
    expose a change record.
    """
    logger.debug("Preparing ansible directory")
    change_record = run_dir + "/change-record.yaml"
    for subdir in ('env', 'project'):
        os.makedirs(run_dir + '/' + subdir)

    # Symlink to git config
    for dirname in ('files', 'filter_plugins', 'templates'):
        if os.path.isdir(git_dir + '/' + dirname):
            os.symlink(
                git_dir + '/' + dirname,
                run_dir + '/project/' + dirname
            )

    # Initialize change record
    with open(change_record, "w") as fh:
        fh.write("# Ansible run started at {}Z\n".format(
            datetime.datetime.utcnow().isoformat()
        ))

    # Define extravars
    extravars = yaml.safe_load(os.environ.get('ANSIBLE_VARS', '{}'))
    extravars.update({
        'openshift_connection_certificate_authority': 
            '/run/secrets/kubernetes.io/serviceaccount/ca.crt',
        'openshift_connection_server': 
            'https://kubernetes.default.svc',
        'openshift_connection_token': 
            get_service_account_token(),
        'openshift_provision_change_record': 
            change_record,
        'openshift_provision_config_path': 
            git_dir
    })
    with open(run_dir + '/env/extravars', 'w') as fh:
        yaml.safe_dump(extravars, fh)

    shutil.copyfile(
        openshift_provision_playbook,
        run_dir + '/project/openshift-provision.yaml'
    )

def do_ansible_run():
    ansible_cmd = [
        "ansible-playbook",
        "--extra-vars=@../env/extravars",
        "openshift-provision.yaml"
    ]

    if is_truthy(os.environ.get('CHECK_MODE', 'no')):
        ansible_cmd.append('--check')

    # FIXME --vault-password-file support?

    ansible_run = subprocess.Popen(
        ansible_cmd,
        cwd = run_dir + '/project',
        env = process_env,
        stderr = subprocess.STDOUT,
        stdout = subprocess.PIPE
    )
    while True:
        output = ansible_run.stdout.readline()
        if output == '' and ansible_run.poll() is not None:
            break
        output = re.sub(r' *\**\n$', '', output)
        if output:
            logger.info('ansible - ' + output)
    logger.info("Return code {}".format(ansible_run.returncode))
    if ansible_run.returncode != 0:
        die('ansible-playbook failed, unable to continue')
    return ansible_run

def git_clone():
    logger.info("Performing git clone")
    git_cmd = [
        'git', 'clone', os.environ['GIT_URL'],
        git_dir
    ]
    git = subprocess.Popen(
        git_cmd,
        env = process_env,
        stderr = subprocess.STDOUT,
        stdout = subprocess.PIPE
    )
    while True:
        output = git.stdout.readline()
        if output == '' and git.poll() is not None:
            break
        output = output.strip()
        if output:
            logger.info("git - " + output)
    logger.info("Return code {}".format(git.returncode))
    if git.returncode != 0:
        die('git clone failed, unable to continue')

def git_checkout():
    logger.info("Performing git checkout")
    git_cmd = [
        'git', 'checkout', os.environ['GIT_REF'],
        git_dir
    ]
    git = subprocess.Popen(
        git_cmd,
        env = process_env,
        cwd = git_dir,
        stderr = subprocess.STDOUT,
        stdout = subprocess.PIPE
    )
    while True:
        output = git.stdout.readline()
        if output == '' and git.poll() is not None:
            break
        output = output.strip()
        if output:
            logger.info("git - " + output)
    logger.info("Return code {}".format(git.returncode))
    if git.returncode != 0:
        die('git clone failed, unable to continue')

def init():
    """Initialization function before management loops."""
    init_nss_wrapper_passwd()
    init_logging()
    check_env()

def init_nss_wrapper_passwd():
    fh = open(nss_wrapper_passwd, 'w')
    fh.write("ansible:x:{}:0:ansible:{}:/bin/bash".format(
        os.geteuid(),
        run_dir
    ))

def init_logging():
    """Define logger global and set default logging level.
    Default logging level is INFO and may be overridden with the
    LOGGING_LEVEL environment variable.
    """
    global logger
    logging.basicConfig(
        format='%(asctime)-15s %(levelname)s - %(message)s',
    )
    logger = logging.getLogger('runner')
    logger.setLevel(os.environ.get('LOGGING_LEVEL', 'INFO'))

def check_env():
    """Check environment for required variables."""
    if 'CALLBACK_URL' not in os.environ:
        die('Environment variable CALLBACK_URL not set'.format(env))
            
def main():
    """Main function."""
    init()
    if os.environ.get('GIT_URL', ''):
        git_clone()
    if os.environ.get('GIT_REF', ''):
        git_checkout()
    run_ansible()
    report_changes()

if __name__ == '__main__':
    main()
