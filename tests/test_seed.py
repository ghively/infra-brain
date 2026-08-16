from scripts.seed_db import parse_discovery_coverage, parse_instincts_dir


def test_parse_discovery_coverage_returns_scan_points(tmp_path):
    yaml_content = """
scan_points:
  - id: linux-all
    domain: linux
    method: ansible
    endpoint: all
    schedule: "0 */2 * * *"
    status: active
  - id: cloud-aws
    domain: cloud
    method: boto3
    endpoint: aws
    schedule: "0 */1 * * *"
    status: active
"""
    f = tmp_path / "discovery-coverage.yml"
    f.write_text(yaml_content)
    result = parse_discovery_coverage(str(f))
    assert len(result) == 2
    assert result[0]["domain"] == "linux"
    assert result[1]["schedule"] == "0 */1 * * *"


def test_parse_instincts_dir_returns_instincts(tmp_path):
    domain_dir = tmp_path / "ansible-authoring"
    domain_dir.mkdir(parents=True)
    instinct_file = domain_dir / "idempotency.yml"
    instinct_file.write_text("""
pattern: "Always use FQCN for Ansible modules"
confidence: 0.95
promoted_by: "infra-ops-v0.6.0"
citation: "rules/ansible/authoring-standards.md"
""")
    result = parse_instincts_dir(str(tmp_path))
    assert len(result) == 1
    assert result[0]["zone"] == "corpor"
    assert result[0]["domain"] == "ansible-authoring"
    assert result[0]["confidence"] == 0.95
