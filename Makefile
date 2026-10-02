.PHONY: test lint typecheck ansible-lint yaml-lint syntax-check check

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
	for playbook in bootstrap verify_deploy_access site update health; do \
		ansible-playbook ansible/playbooks/$$playbook.yml --syntax-check -i tests/fixtures/inventory.yml; \
	done

check: test lint typecheck yaml-lint ansible-lint syntax-check
