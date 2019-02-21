# openshift-provision-manager

Containerized cluster self-management using the
[openshift-provision](https://github.com/gnuthought/ansible-role-openshift-provision) ansible role.

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
oc process -p OPENSHIFT_PROVISION_NAMESPACE=$NAMESPACE -f install-template-nonadmin.yaml | oc create -f -
```
