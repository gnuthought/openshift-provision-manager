FROM centos:7
ARG OPENSHIFT_PROVISION_REPO=https://github.com/gnuthought/ansible-role-openshift-provision

USER 0

RUN yum install -y \
      ansible \
      gcc \
      git \
      python \
      python-devel \
      python-jmespath \
      python-setuptools && \
    yum clean all

RUN easy_install pip && \
    pip install --ignore-installed \
      ansible-runner \
      kubernetes && \
    git clone --branch=master --single-branch \
      ${OPENSHIFT_PROVISION_REPO} /etc/ansible/roles/openshift-provision

COPY manager.py /opt/openshift-provision/manager.py
COPY openshift-provision.yaml /opt/openshift-provision/openshift-provision.yaml

CMD /opt/openshift-provision/manager.py
