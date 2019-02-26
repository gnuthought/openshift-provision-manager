# openshift-provision-manager

Containerized cluster self-management using the
[openshift-provision](https://github.com/gnuthought/ansible-role-openshift-provision) ansible role.

The openshift-provision-manager is a tool for provisioning resources into an
OpenShift cluster by running a manager in the target cluster which can respond
to configuration changes and git webhooks to implement GitOps.

## Project Goals

In addition to the project goals of the underlying openshift-provision Ansible
role, this containerized deployment adds:

* **Usable** - without requiring cluster admin access, no custom resource
  definitions required.

* **Versioned** - integrated with git version control

* **Pushable** - configmaps are processed immediately and webhooks are
  available for version control triggers

* **Polling** - resources are periodically checked for configuration
  divergence and configurable convergence

* **Immediate** - responsive to changes in managed resources (feature in
  project roadmap)

## Is this an Operator?

Yes and no. The openshift-provision-manager does not use the
[operator-sdk](https://github.com/operator-framework/operator-sdk) and does
not use custom resource definitions (CRDs). Instead it watches the standard
ConfigMap resource type using label selectors to respond to changes in a manner
similar to how operators use CRDs. Using ConfigMaps means that no special admin
level access is needed to deploy the openshift-provision-manager beyond access
to manage resources in target namespaces.

## Examples

In order of increasing complexity:

All-in-one declarative resource management:
[openshift-provision-example-0](https://github.com/gnuthought/openshift-provision-example-0)

Add using git clone to keep in sync with a repository:
[openshift-provision-example-1](https://github.com/gnuthought/openshift-provision-example-1)

Add using OpenShift templates to simplify managing multiple similar resources:
[openshift-provision-example-2](https://github.com/gnuthought/openshift-provision-example-2)

Each template includes installation templates used in a similar manner as
described below.

## Installation

As a cluster administrator:

```
oc process -f install-template-admin.yaml | oc create -f -
```

If installing as a non-administrator you will need to pre-create a namespace
for the openshift-provision-manager:

```
PROVISIONER_NAMESPACE=example-provisioner
oc new-project $PROVISIONER_NAMESPACE
oc process -f install-template-nonadmin.yaml \
 -p OPENSHIFT_PROVISION_NAMESPACE=$PROVISIONER_NAMESPACE \
| oc create -f -
```

## Configuration

The openshift-provision-manager watches for configuration with config maps in
its namespace with the label "openshift-provision.gnuthought.com/config=true".
Each config map may specify:

* **git_url** - Git repository to checkout for resource configuration (optional)

* **git_ref** - Git ref to checkout from git repository (default 'master')

* **config_path** - Space separated list of YAML files for ansible variables
  to configure openshift-provision. Earlier items in the list may override
  variables from later entries. Paths are relative to the base of the git
  repository. (required if `git_url` is provided)

* **check_mode** - Sets ansible check mode (default 'no')

* **vars** - YAML formatted variable definitions. If a definition of
  `openshift_provision` is given then no git settings are required.

* **service_account** - Service account used for provisioning cluster
  resources. A service account with this name must exist in the same namespace
  as the openshift-provision-manager. This service account must be granted
  appropriate access to provision resources given by configuration.

* **webhook_key** - Key value used to call manager webhooks for this
  configuration item. Use `webhook_secret` if security is a concern.

* **webhook_secret** - Secret name in which the webhook key is stored. The
  secret must exist and have the key in the field named `key`.

* **initial_delay** - Initial delay after configuration is added or updated
  (default '5s')

* **recheck_interval** - Interval for ansible runs after a check mode run finds
  differences or configuration change is provisioned (default '5m')
  
* **retry_interval** - Interval for ansible runs after ansible pod failure
  (default '10m')

* **run_interval** - Interval for ansible runs after success indicating no
  changes (default '30m')

* **run_timeout** - Maximum time period to wait for ansible pod completion
  (default '10m')
