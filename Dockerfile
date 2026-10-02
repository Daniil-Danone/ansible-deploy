FROM python:3.12.10-slim-bookworm

ARG ANSIBLE_CORE_VERSION=2.18.6
ARG ANSIBLE_LINT_VERSION=25.4.0
ARG YAMLLINT_VERSION=1.37.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir \
      "ansible-core==${ANSIBLE_CORE_VERSION}" \
      "ansible-lint==${ANSIBLE_LINT_VERSION}" \
      "yamllint==${YAMLLINT_VERSION}"

WORKDIR /workspace
COPY ansible/requirements.yml /tmp/requirements.yml
RUN ansible-galaxy collection install -r /tmp/requirements.yml

ENTRYPOINT ["ansible-playbook"]
