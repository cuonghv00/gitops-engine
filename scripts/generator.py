#!/usr/bin/env python3
"""
generator.py
============
The Python "Engine" for the Helm Monorepo Automation System.

Reads apps_definition.yaml (Single Source of Truth) and for each app:
  1. Creates/updates the directory at charts/<app-name>/
  2. Generates Chart.yaml with a file:// dependency on common-lib
  3. Generates values.yaml mapping app config to common-lib value keys
  4. Creates an empty images.yaml if it does not already exist (CI writes to it)

Design principle: IDEMPOTENT — safe to run multiple times without side effects.

Usage:
  # Single-project mode (root-level, default):
  python3 scripts/generator.py [--dry-run]

  # Multi-project mode (one sub-directory per project under projects/):
  python3 scripts/generator.py --project <name> [--dry-run]

  # Advanced: fully custom paths (overrides --project):
  python3 scripts/generator.py --definition PATH --output-dir DIR [--dry-run]

Requirements:
  pip install -r scripts/requirements.txt
"""

import argparse
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Paths (relative to repo root, resolved at runtime)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
APPS_DEFINITION_DEFAULT = REPO_ROOT / "apps_definition.yaml"
COMMON_LIB_PATH = REPO_ROOT / "helm-templates" / "common-lib"

# Global state updated in main()
CHARTS_DIR = REPO_ROOT / "charts"
COMMON_LIB_REL = "../../helm-templates/common-lib"


# ---------------------------------------------------------------------------
# Template builders
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Template builders
# ---------------------------------------------------------------------------

def parse_env_file(env_path: Path) -> tuple[dict, list]:
    """
    Parses a .env file and classifies variables:
    - Literals (KEY=VALUE) -> ConfigMap
    - Placeholders (KEY=${VAR}) -> Secret
    """
    config_data = {}
    secret_keys = []
    
    if not env_path.exists():
        return config_data, secret_keys

    import re
    # Pattern to match KEY=VALUE
    pattern = re.compile(r"^\s*([\w.-]+)\s*=\s*(.*)\s*$")
    
    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            match = pattern.match(line)
            if match:
                key, value = match.groups()
                # Remove quotes if present
                value = value.strip("'\"")
                
                if value.startswith("${") and value.endswith("}"):
                    secret_keys.append(key)
                else:
                    config_data[key] = value
                    
    return config_data, secret_keys


def build_chart_yaml(app_name: str, common_version: str, is_shared: bool = False) -> dict:
    """
    Builds the Chart.yaml content for a child application chart or shared chart.
    """
    desc = f"Shared resources for project {app_name}" if is_shared else f"Application chart for {app_name}"
    return {
        "apiVersion": "v2",
        "name": app_name,
        "description": f"{desc} — managed by generator.py",
        "type": "application",
        "version": "0.1.0",
        "dependencies": [
            {
                "name": "common-lib",
                "version": common_version,
                "repository": f"file://{COMMON_LIB_REL}",
                "alias": "common-lib",
            }
        ],
    }


def build_values_yaml(app: dict, global_defaults: dict, project_vars: tuple[dict, list]) -> dict:
    """
    Maps the flat app config from apps_definition.yaml to the nested
    values structure expected by common-lib templates.
    """
    config_pool, secret_pool = project_vars
    project_name = global_defaults.get("project", "default")
    
    # 1. Merge nested 'config' and first item from 'containers' for flexibility
    cfg = app.get("config", {})
    
    # Handle 'containers' list if it exists (K8s style)
    container_cfg = {}
    if "containers" in app:
        containers = app["containers"]
        if isinstance(containers, list) and len(containers) > 0:
            container_cfg = containers[0]
        elif isinstance(containers, dict): # Handle if operator forgot to use a list
            container_cfg = containers

    full_app = {**app, **cfg, **container_cfg}
    
    app_name = full_app.get("name")
    
    # 2. Image construction
    image_repo = full_app.get("image_repo") or global_defaults.get("image_repo", "registry.example.com")
    image_override = full_app.get("image")
    tag = full_app.get("image_tag") or global_defaults.get("image_tag", "latest")
    
    # Ignore placeholder strings
    if image_override and "điền tên" in image_override:
        image_override = None

    if image_override:
        image_name = image_override
    else:
        image_name = f"{image_repo}/{app_name}"

    # 3. Base Security Context (Maximum Hardening)
    default_sec_ctx = {
        "readOnlyRootFilesystem": True,
        "allowPrivilegeEscalation": False,
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "capabilities": {"drop": ["ALL"]}
    }
    
    # Merge override logic
    sec_ctx = {**default_sec_ctx, **full_app.get("securityContext", {})}

    # 4. Volumes and Temp Mount
    volumes = full_app.get("volumes", [])
    volume_mounts = full_app.get("volumeMounts", [])
    
    # Auto-mount /tmp by default to prevent application crash on R/O FS
    if sec_ctx.get("readOnlyRootFilesystem") and full_app.get("auto_mount_tmp", True):
        tmp_vol_name = "tmp"
        if not any(v.get("name") == tmp_vol_name for v in volumes):
            volumes.append({"name": tmp_vol_name, "emptyDir": {}})
            volume_mounts.append({"name": tmp_vol_name, "mountPath": "/tmp"})

    # Check if app needs to mount the whole shared env file
    if full_app.get("mount_env_file"):
        cm_name = f"{project_name}-config"
        volume_mounts.append({
            "name": "env-file",
            "mountPath": "/app/.env",
            "subPath": ".env"
        })
        volumes.append({
            "name": "env-file",
            "configMap": {"name": cm_name}
        })

    # 5. Health Check (v3 Hierarchical Logic)
    health_cfg = full_app.get("health", {})
    health_enabled = health_cfg.get("enabled", False)
    
    liveness = None
    readiness = None

    if health_enabled:
        app_port = full_app.get("port", 80)
        
        # Base parameters for all probes
        base_params = {
            "path": health_cfg.get("path"),  # No default here, let build_probe decide
            "port": health_cfg.get("port") or app_port,
            "initialDelaySeconds": health_cfg.get("initialDelaySeconds", 10),
            "periodSeconds": health_cfg.get("periodSeconds", 5),
            "timeoutSeconds": health_cfg.get("timeoutSeconds", 2),
            "failureThreshold": health_cfg.get("failureThreshold", 3)
        }

        def build_probe(type_key: str):
            # Check for top-level probe definition first (v1 style)
            explicit_probe = full_app.get(f"{type_key}Probe")
            if explicit_probe:
                return explicit_probe

            # Helper to merge: base_params < health_cfg < health[type_key]
            type_cfg = health_cfg.get(type_key, {})
            if isinstance(type_cfg, str): # Handle short-form: liveness: "/healthz"
                type_cfg = {"path": type_cfg}
            
            merged = {**base_params, **type_cfg}
            
            probe_def = {
                "initialDelaySeconds": merged["initialDelaySeconds"],
                "periodSeconds": merged["periodSeconds"],
                "timeoutSeconds": merged["timeoutSeconds"],
                "failureThreshold": merged["failureThreshold"]
            }

            # Switch between HTTP and TCP based on presence of path
            if merged.get("path"):
                probe_def["httpGet"] = {"path": merged["path"], "port": merged["port"]}
            else:
                probe_def["tcpSocket"] = {"port": merged["port"]}

            return probe_def

        liveness = build_probe("liveness")
        readiness = build_probe("readiness")

    # 6. Environment Variables Mapping (Shared Resources aware)
    # ... (rest of the logic remains same, just ensuring we don't output nulls later)

    # 6. Environment Variables Mapping (Shared Resources aware)
    env_list = full_app.get("env", [])
    env_keys = full_app.get("env_vars", []) # New simplified key
    
    # 6. Service Configuration (Smart Defaults)
    svc_cfg = full_app.get("service", {})
    ingress_cfg = full_app.get("ingress", {})
    app_port = full_app.get("port", 80)
    
    # Auto-enable service if ingress is present or service block is present
    if (ingress_cfg.get("enabled") or svc_cfg) and not svc_cfg:
        svc_cfg = {}

    if svc_cfg or ingress_cfg.get("enabled"):
        # Port precedence: service.port > ingress.servicePort > app_port
        resolved_svc_port = svc_cfg.get("port") or ingress_cfg.get("servicePort") or app_port
        target_port = svc_cfg.get("targetPort") or app_port
        
        new_svc_cfg = {
            "type": svc_cfg.get("type", "ClusterIP"),
            "port": resolved_svc_port,
            "targetPort": target_port
        }
        
        if new_svc_cfg["type"] == "NodePort" and "nodePort" in svc_cfg:
            new_svc_cfg["nodePort"] = svc_cfg["nodePort"]
            
        svc_cfg = new_svc_cfg
        svc_final_port = resolved_svc_port
    else:
        svc_final_port = 80

    container_port = app_port

    # 7. Ingress Transformation (Simplified -> Standard)
    if ingress_cfg.get("enabled"):
        # If user used the simplified 'host' field, convert it to the library standard
        if "host" in ingress_cfg and "hosts" not in ingress_cfg:
            simple_host = ingress_cfg.pop("host")
            ingress_cfg["hosts"] = [{
                "host": simple_host,
                "paths": [{
                    "path": "/",
                    "pathType": "ImplementationSpecific"
                }]
            }]
        
        # Ensure ingress points to the correct service port
        if "hosts" in ingress_cfg:
            for host_entry in ingress_cfg["hosts"]:
                for path_entry in host_entry.get("paths", []):
                    path_entry["servicePort"] = svc_final_port

    # 8. Shared Resources via envFrom (ConfigMap only as requested)
    # Start with the smart default (shared project config)
    env_from = [
        {"configMapRef": {"name": f"{project_name}-config"}}
    ]
    
    # Allow additional envFrom entries from the app definition
    extra_env_from = full_app.get("envFrom", [])
    if isinstance(extra_env_from, list):
        env_from.extend(extra_env_from)

    # 9. Environment Variables Mapping (Secrets & Overrides)
    env_list = full_app.get("env", [])
    env_keys = full_app.get("env_vars", [])
    
    for key in env_keys:
        # We keep secrets explicit in the env list as requested
        if key in secret_pool:
            env_list.append({
                "name": key,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": f"{project_name}-secret",
                        "key": key
                    }
                }
            })
        # Note: We skip config_pool keys because they are already covered by envFrom above

    # 10. Image Pull Secrets
    # Priority: app.imagePullSecrets > global_defaults.imagePullSecrets > default [{"name": "regcred"}]
    image_pull_secrets = full_app.get("imagePullSecrets") or global_defaults.get("imagePullSecrets") or [{"name": "regcred"}]

    deployment_data = {
        "replicas": full_app.get("replicas", 1),
        "containerPort": container_port,
        "resources": full_app.get("resources", {}),
        "strategy": {"type": full_app.get("strategy", "RollingUpdate")},
        "securityContext": sec_ctx,
        "imagePullSecrets": image_pull_secrets,
        "envFrom": env_from,
        "env": env_list,
        "volumes": volumes,
        "volumeMounts": volume_mounts,
        "affinity": full_app.get("affinity", {})
    }
    
    # Only add probes if they are configured
    if liveness:
        deployment_data["livenessProbe"] = liveness
    if readiness:
        deployment_data["readinessProbe"] = readiness

    values: dict = {
        "type": full_app.get("type", "deployment"),
        "image": {
            "repository": image_name,
            "tag": tag,
            "pullPolicy": full_app.get("pullPolicy", "IfNotPresent"),
        },
        "deployment": deployment_data,
        "serviceAccount": {
            "create": False,
            "name": full_app.get("serviceAccount", "default"),
        },
        "service": svc_cfg,
        "ingress": full_app.get("ingress", {}),
        "localConfig": {
            "enabled": full_app.get("genConfigMaps", False)
        }
    }

    return values


def build_images_yaml() -> dict:
    """
    Stub file written once. CI pipeline updates image.tag here via
    the GitLab Commits API without touching values.yaml (avoids merge conflicts).
    """
    return {
        "image": {
            "tag": "latest",
        }
    }


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def write_yaml(path: Path, data: dict, dry_run: bool = False) -> None:
    """Writes a dict as YAML to the given path. Skips write in dry-run mode."""
    content = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    if dry_run:
        print(f"  [DRY-RUN] Would write: {path}")
        print("  " + content.replace("\n", "\n  "))
    else:
        path.write_text(content)
        print(f"  [WRITE]   {path}")


def ensure_dir(path: Path, dry_run: bool = False) -> None:
    """Creates a directory (and parents) if it doesn't exist."""
    if not path.exists():
        if dry_run:
            print(f"  [DRY-RUN] Would create directory: {path}")
        else:
            path.mkdir(parents=True, exist_ok=True)
            print(f"  [MKDIR]   {path}")
    else:
        print(f"  [EXISTS]  {path}")


# ---------------------------------------------------------------------------
# Core generator logic
# ---------------------------------------------------------------------------

ALL_YAML_CONTENT = """\
{{/*
  all.yaml — Auto-generated by scripts/generator.py. Do NOT edit manually.
  Calls the common-lib.main router which renders the correct Kubernetes
  resource based on .Values.type defined in values.yaml.
*/}}
{{ include "common-lib.main" . }}
"""


def generate_shared_chart(global_defaults: dict, project_vars: tuple[dict, list], dry_run: bool = False) -> None:
    """Generates the project-shared chart containing shared ConfigMap and Secret template."""
    project_name = global_defaults["project"]
    common_version = global_defaults["common_version"]
    config_pool, secret_pool = project_vars
    
    chart_dir = CHARTS_DIR / "project-shared"
    
    print(f"\n▶  Generating shared resources: project-shared")
    ensure_dir(chart_dir, dry_run)
    ensure_dir(chart_dir / "templates", dry_run)
    
    # 1. Chart.yaml
    write_yaml(chart_dir / "Chart.yaml", build_chart_yaml(f"{project_name}-shared", common_version, is_shared=True), dry_run)
    
    # 2. ConfigMap template
    cm_data = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{project_name}-config",
        },
        "data": config_pool
    }
    write_yaml(chart_dir / "templates" / "configmap.yaml", cm_data, dry_run)
    
    # 3. Secret template (Contains placeholders, will be updated by vault_sync.sh)
    # We create a dummy secret with the keys but empty values or placeholders
    secret_data = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"{project_name}-secret",
        },
        "type": "Opaque",
        "data": {key: "" for key in secret_pool}
    }
    write_yaml(chart_dir / "templates" / "secret.yaml", secret_data, dry_run)


def generate_chart(app: dict, global_defaults: dict, project_vars: tuple[dict, list], dry_run: bool = False) -> None:
    """Generates or updates the Helm chart directory for a single app."""
    app_name: str = app["name"]
    chart_dir = CHARTS_DIR / app_name
    common_version = global_defaults.get("common_version", "1.0.0")

    print(f"\n▶  Processing app: {app_name} (type={app.get('type', 'deployment')})")

    # 1. Create chart directory
    ensure_dir(chart_dir, dry_run)

    # 2. Create templates/ with the single router-call file (all.yaml)
    ensure_dir(chart_dir / "templates", dry_run)

    # Write all.yaml — always overwrite since it's fully auto-generated
    all_yaml_path = chart_dir / "templates" / "all.yaml"
    if dry_run:
        print(f"  [DRY-RUN] Would write: {all_yaml_path}")
    else:
        all_yaml_path.write_text(ALL_YAML_CONTENT)
        print(f"  [WRITE]   {all_yaml_path}")

    # 3. Generate Chart.yaml (always overwrite — version is managed by this script)
    chart_yaml_path = chart_dir / "Chart.yaml"
    write_yaml(chart_yaml_path, build_chart_yaml(app_name, common_version), dry_run)

    # 4. Generate values.yaml (always overwrite — source of truth is apps_definition.yaml)
    values_yaml_path = chart_dir / "values.yaml"
    write_yaml(values_yaml_path, build_values_yaml(app, global_defaults, project_vars), dry_run)

    # 5. Create images.yaml ONLY if it doesn't exist — CI owns this file after first creation
    images_yaml_path = chart_dir / "images.yaml"
    if not images_yaml_path.exists():
        write_yaml(images_yaml_path, build_images_yaml(), dry_run)
    else:
        print(f"  [SKIP]    {images_yaml_path} (already exists — owned by CI)")


def main(definition_path: Path, output_dir: Path, dry_run: bool) -> None:
    """Entry point: loads app definitions and runs the generator for each app."""
    global CHARTS_DIR, COMMON_LIB_REL

    if not definition_path.exists():
        print(f"ERROR: Definition file not found: {definition_path}", file=sys.stderr)
        sys.exit(1)

    CHARTS_DIR = output_dir.resolve()
    
    try:
        import os
        dummy_app_path = CHARTS_DIR / "dummy-app"
        COMMON_LIB_REL = os.path.relpath(COMMON_LIB_PATH, dummy_app_path)
    except Exception as e:
        print(f"WARNING: Could not calculate relative path to common-lib: {e}")

    with definition_path.open() as f:
        data = yaml.safe_load(f)

    global_defaults = {
        "project": data.get("project", "unknown"),
        "common_version": data.get("common_version", "1.0.0"),
        "namespace": data.get("namespace", "default"),
        "image_repo": data.get("image_repo"),
        "image_tag": data.get("image_tag"),
        "imagePullSecrets": data.get("imagePullSecrets"),
    }
    apps = data.get("apps", [])

    # Load and parse variables.env if it exists in the same directory as definition
    env_path = definition_path.parent / "variables.env"
    project_vars = parse_env_file(env_path)
    
    print(f"=== Helm Monorepo Generator ===")
    print(f"Project         : {global_defaults['project']}")
    print(f"Environment Info: {env_path.name if env_path.exists() else 'None'}")
    print(f"Config keys     : {len(project_vars[0])}")
    print(f"Secret keys     : {len(project_vars[1])}")
    print(f"Apps to process : {len(apps)}")
    print(f"Dry-run mode    : {dry_run}")
    print(f"Output dir      : {CHARTS_DIR}")

    if not apps:
        print("\nWARNING: No apps found in definition file. Nothing to generate.")
        return

    # 1. Generate shared resources chart if we have project variables
    if project_vars[0] or project_vars[1]:
        generate_shared_chart(global_defaults, project_vars, dry_run)

    # 2. Generate each application chart
    for app in apps:
        if "name" not in app:
            print(f"\nERROR: Skipping invalid app entry (missing 'name'): {app}", file=sys.stderr)
            continue
        generate_chart(app, global_defaults, project_vars, dry_run)

    print(f"\n✅  Done. Generated resources in {CHARTS_DIR}/")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Helm Monorepo Generator — scaffolds Helm charts from an apps definition YAML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Root-level project (default):
  python3 scripts/generator.py

  # One of several sub-projects:
  python3 scripts/generator.py --project ecommerce

  # Dry-run to preview changes without writing files:
  python3 scripts/generator.py --project fintech --dry-run
""",
    )
    parser.add_argument(
        "--project",
        metavar="NAME",
        default=None,
        help=(
            "Sub-project name under the projects/ directory. "
            "Auto-sets --definition to projects/NAME/apps.<env>.yaml "
            "and --output-dir to projects/NAME/charts/. "
            "Omit to use root-level definition (single-project mode)."
        ),
    )
    parser.add_argument(
        "--env",
        metavar="ENV",
        default="dev",
        help="Environment name (e.g., dev, staging, prod). Defaults to 'dev'.",
    )
    parser.add_argument(
        "--definition",
        type=Path,
        default=None,
        help="Override path to the apps definition YAML file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override output directory for generated charts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without writing any files.",
    )
    args = parser.parse_args()

    # Resolve paths based on --project shortcut or explicit overrides
    definition_filename = f"apps.{args.env}.yaml"
    
    if args.project:
        project_root = REPO_ROOT / "projects" / args.project
        resolved_definition = args.definition or (project_root / definition_filename)
        resolved_output = args.output_dir or (project_root / "charts")
    else:
        resolved_definition = args.definition or (REPO_ROOT / definition_filename)
        resolved_output = args.output_dir or (REPO_ROOT / "charts")

    main(resolved_definition, resolved_output, args.dry_run)
