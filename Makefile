.PHONY: test lint typecheck ansible-lint yaml-lint syntax-check integration-test check

test:
	python -m pytest

lint:
	python -m ruff check .

typecheck:
	python -m mypy

ansible-lint:
	ANSIBLE_ROLES_PATH=ansible/roles ansible-lint ansible

yaml-lint:
	yamllint .

syntax-check:
	for playbook in bootstrap verify_deploy_access guard_environment site update health \
		finalize_release abort_release rollback; do \
		ansible-playbook ansible/playbooks/$$playbook.yml --syntax-check -i tests/fixtures/inventory.yml; \
	done

integration-test:
	set -e; for playbook in release_finalize release_restore legacy_snapshot identity_guard \
		legacy_guards; do \
		docker run --rm -e ANSIBLE_ROLES_PATH=/workspace/ansible/roles \
			-v "$(CURDIR):/workspace" -w /workspace ansible-deploy:local \
			tests/integration/$$playbook.yml -i localhost, -v; \
	done

check: test lint typecheck yaml-lint ansible-lint syntax-check
