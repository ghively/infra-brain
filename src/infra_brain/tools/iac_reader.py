"""
IaC reader tools — parse infrastructure-as-code files from GitLab repos.
Read-only: only reads file content, never modifies.
"""

import re
import yaml
from langchain_core.tools import tool
from infra_brain.tools.gitlab import gitlab_file_tool, gitlab_repository_tree_tool

_IAC_EXTENSIONS = {".yml", ".yaml", ".tf", ".tofu", ".tfvars"}
_TF_RESOURCE_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE)


@tool
def find_iac_files_tool(project_id: int, ref: str = "main", path: str = "") -> list[dict]:
    """
    Find all IaC files in a GitLab repository (read-only).
    Returns list of {path, name, type} for .yml/.yaml/.tf/.tofu files.
    """
    tree = gitlab_repository_tree_tool.invoke(
        {"project_id": project_id, "path": path, "ref": ref, "recursive": True}
    )
    return [
        {"path": item["path"], "name": item["name"], "type": item["type"]}
        for item in tree
        if item["type"] == "blob" and any(item["name"].endswith(ext) for ext in _IAC_EXTENSIONS)
    ]


@tool
def read_iac_file_tool(project_id: int, file_path: str, ref: str = "main") -> str:
    """Read an IaC file from a GitLab repository (read-only). Returns file content as string."""
    return gitlab_file_tool.invoke({"project_id": project_id, "file_path": file_path, "ref": ref})


@tool
def parse_ansible_inventory_content_tool(content: str) -> dict:
    """
    Parse Ansible inventory YAML content into a normalized {groups, hosts} structure.
    Handles the YAML inventory format (groups with hosts and hostvars).
    Returns: {"groups": {group_name: [hostnames]}, "hosts": {hostname: {vars}}}
    """
    if not content.strip():
        return {"groups": {}, "hosts": {}}
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return {"groups": {}, "hosts": {}, "error": "invalid YAML"}

    if not isinstance(data, dict):
        return {"groups": {}, "hosts": {}}

    groups: dict = {}
    hosts: dict = {}

    def _walk(node: dict, parent: str | None = None):
        if not isinstance(node, dict):
            return
        if "hosts" in node and isinstance(node["hosts"], dict):
            group_hosts = list(node["hosts"].keys())
            if parent:
                groups.setdefault(parent, []).extend(group_hosts)
            for hostname, hostvars in node["hosts"].items():
                hosts[hostname] = hostvars or {}
        if "children" in node and isinstance(node["children"], dict):
            for child_name, child_node in node["children"].items():
                _walk(child_node or {}, child_name)

    # Top-level is usually "all"
    if "all" in data:
        _walk(data["all"])
    else:
        for group_name, group_data in data.items():
            _walk(group_data or {}, group_name)

    return {"groups": groups, "hosts": hosts}


@tool
def parse_terraform_resources_tool(content: str) -> list[dict]:
    """
    Extract resource blocks from Terraform/OpenTofu HCL content (read-only).
    Returns list of {resource_type, resource_name}.
    """
    matches = _TF_RESOURCE_RE.findall(content)
    return [{"resource_type": rtype, "resource_name": rname} for rtype, rname in matches]


# --- Stage-2 parsers: structured child-row extraction (read-only) ----------
#
# These return the typed fields the relational child tables need. They were
# added when the IaC collector started populating compose_services /
# k8s_manifest_resources / ansible_playbook_plays — the original collector only
# kept summary counts, so the per-service / per-resource / per-play detail was
# never extracted. All parsing is read-only string/YAML inspection.


@tool
def parse_compose_services_tool(content: str) -> list[dict]:
    """Parse a Docker Compose file into a list of service rows (read-only).

    Returns ``[{service_name, image, ports: [...], config: {...}}]`` where
    ``config`` is the remaining service definition (everything except image/ports)
    for the ``details`` JSONB column. Returns ``[]`` for unparseable / non-compose
    content.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    services = data.get("services")
    if not isinstance(services, dict):
        return []

    rows: list[dict] = []
    for name, spec in services.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            spec = {}
        ports = spec.get("ports") or []
        if not isinstance(ports, list):
            ports = [ports]
        # Normalize port entries to strings (compose allows int short-form / mappings).
        norm_ports = [str(p) for p in ports]
        config = {k: v for k, v in spec.items() if k not in ("image", "ports")}
        rows.append(
            {
                "service_name": str(name),
                "image": spec.get("image"),
                "ports": norm_ports,
                "config": config,
            }
        )
    return rows


@tool
def parse_k8s_resources_tool(content: str) -> list[dict]:
    """Parse one K8s manifest (possibly multi-document) into resource rows (read-only).

    Returns ``[{kind, api_version, name, namespace, labels, annotations}]`` — one
    per YAML document that has both ``kind`` and ``apiVersion``. ``labels`` /
    ``annotations`` come from ``metadata`` and feed the ``details`` JSONB column.
    Returns ``[]`` for unparseable content.
    """
    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError:
        return []

    rows: list[dict] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if "kind" not in doc or "apiVersion" not in doc:
            continue
        meta = doc.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        rows.append(
            {
                "kind": str(doc.get("kind")),
                "api_version": doc.get("apiVersion"),
                "name": str(meta.get("name") or ""),
                "namespace": meta.get("namespace"),
                "labels": meta.get("labels") or {},
                "annotations": meta.get("annotations") or {},
            }
        )
    return rows


@tool
def parse_ansible_playbook_plays_tool(content: str) -> list[dict]:
    """Parse an Ansible playbook into a list of play rows (read-only).

    Returns ``[{play_index, name, hosts: [...]}]`` — one per play (top-level list
    item). ``hosts`` is always normalized to a list (Ansible allows a single host
    pattern string or a list). Returns ``[]`` for non-playbook / unparseable
    content.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return []
    if not isinstance(data, list):
        return []

    rows: list[dict] = []
    for idx, play in enumerate(data):
        if not isinstance(play, dict) or "hosts" not in play:
            continue
        hosts = play.get("hosts")
        if isinstance(hosts, list):
            host_list = [str(h) for h in hosts]
        elif hosts is None:
            host_list = []
        else:
            host_list = [str(hosts)]
        rows.append(
            {
                "play_index": idx,
                "name": play.get("name"),
                "hosts": host_list,
            }
        )
    return rows
