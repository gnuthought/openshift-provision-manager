FROM centos:7
ARG OPENSHIFT_PROVISION_REPO=https://github.com/gnuthought/ansible-role-openshift-provision
ARG HELM_URL=https://storage.googleapis.com/kubernetes-helm/helm-v2.13.0-linux-amd64.tar.gz

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
    yum upgrade -y && \
    yum clean all && \
    useradd -u 1000 ansible

RUN easy_install pip && \
    pip install --ignore-installed \
      ansible==2.6.14 \
      requests \
    && \
    curl -o helm.tgz $HELM_URL && \
    tar xvf helm.tgz --strip=1 --directory=/usr/bin */helm && \
    git clone --branch=master --single-branch \
      ${OPENSHIFT_PROVISION_REPO} /etc/ansible/roles/openshift-provision

COPY runner.py openshift-provision.yaml /opt/openshift-provision/

CMD /opt/openshift-provision/runner.py
