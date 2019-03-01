FROM centos:7
ARG OPENSHIFT_PROVISION_REPO=https://github.com/gnuthought/ansible-role-openshift-provision

USER 0

RUN yum install -y \
      centos-release-openshift-origin311 \
      epel-release \
      git \
      python \
      python-devel \
      python-jmespath \
      python-setuptools \
    && \
    yum install -y \
      nss_wrapper \
      origin-clients \
    && \
    yum clean all && \
    useradd -u 1000 ansible

RUN easy_install pip && \
    pip install --ignore-installed \
      ansible==2.6.14 \
      requests \
    && \
    git clone --branch=master --single-branch \
      ${OPENSHIFT_PROVISION_REPO} /etc/ansible/roles/openshift-provision

COPY runner.py openshift-provision.yaml /opt/openshift-provision/

CMD /opt/openshift-provision/runner.py
