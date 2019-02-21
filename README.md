# openshift-provision-manager

Containerized cluster self-management using the
[openshift-provision](https://github.com/gnuthought/ansible-role-openshift-provision) ansible role.

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
  project resources)

## Examples

In order of increasing complexity:

All-in-one declarative resource management:
[openshift-provision](https://github.com/gnuthought/openshift-provision-example-0)

Add using git clone to keep in sync with a repository:
[openshift-provision](https://github.com/gnuthought/openshift-provision-example-1)

Add using OpenShift templates to simplify managing multiple similar resources:
[openshift-provision](https://github.com/gnuthought/openshift-provision-example-2)

Each template includes installation templates used in a similar manner as
described below.

## Installation

As a cluster administrator:

```
oc process -f install-template-admin.yaml | oc create -f -
```

If installing an a non-administrator:

```
NAMESPACE=example-provisioner
oc new-project $NAMESPACE
oc process -f install-template-nonadmin.yaml \
 -p OPENSHIFT_PROVISION_NAMESPACE=$NAMESPACE \
| oc create -f -
```
