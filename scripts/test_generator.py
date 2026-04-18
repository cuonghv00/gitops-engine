#!/usr/bin/env python3
"""
test_generator.py
=================
Validation tests for the refactored generator.py.
Covers: ProjectPVC, EnvItem, VolumeItem, build_values_yaml output.
"""
import sys
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.generator import (
    ProjectDefinition, AppConfig, EnvItem, VolumeItem, ProjectPVC, ServicePort,
    build_values_yaml, build_env_items, build_volume_items, build_project_pvcs_yaml,
    parse_env_file, _validate_k8s_name,
)

PASS = "✅ PASS"
FAIL = "❌ FAIL"


def test(name: str, cond: bool, detail: str = "") -> bool:
    status = PASS if cond else FAIL
    msg = f"  {status}  {name}"
    if not cond and detail:
        msg += f"\n         → {detail}"
    print(msg)
    return cond


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


results = []

# ===========================================================================
# 1. EnvItem validation
# ===========================================================================
section("1. EnvItem — Validation")

# Valid: plain value
try:
    e = EnvItem(name="LOG", value="debug")
    results.append(test("plain name/value", e.name == "LOG"))
except Exception as ex:
    results.append(test("plain name/value", False, str(ex)))

# Valid: secretEnv
try:
    e = EnvItem(secretEnv="my-secret", vars=["KEY1", "KEY2"])
    results.append(test("secretEnv + vars", e.secretEnv == "my-secret" and e.vars == ["KEY1", "KEY2"]))
except Exception as ex:
    results.append(test("secretEnv + vars", False, str(ex)))

# Valid: configMap envFrom
try:
    e = EnvItem(configMap="global-cfg")
    results.append(test("configMap envFrom", e.configMap == "global-cfg"))
except Exception as ex:
    results.append(test("configMap envFrom", False, str(ex)))

# Valid: secret envFrom
try:
    e = EnvItem(secret="api-keys")
    results.append(test("secret envFrom", e.secret == "api-keys"))
except Exception as ex:
    results.append(test("secret envFrom", False, str(ex)))

# Valid: k8s (native K8s EnvVar)
try:
    e = EnvItem(k8s={"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}})
    results.append(test("k8s env", "name" in e.k8s))
except Exception as ex:
    results.append(test("k8s env", False, str(ex)))

# Invalid: multiple sources (name + k8s)
try:
    e = EnvItem(name="X", configMap="Y")
    results.append(test("multiple sources → should fail", False, "No error raised"))
except Exception:
    results.append(test("multiple sources → should fail", True))

# Invalid: secretEnv without vars
try:
    e = EnvItem(secretEnv="sec")
    results.append(test("secretEnv without vars → should fail", False, "No error raised"))
except Exception:
    results.append(test("secretEnv without vars → should fail", True))

# Invalid: empty item
try:
    e = EnvItem()
    results.append(test("empty EnvItem → should fail", False, "No error raised"))
except Exception:
    results.append(test("empty EnvItem → should fail", True))


# ===========================================================================
# 2. build_env_items
# ===========================================================================
section("2. build_env_items — Output")

envs = [
    EnvItem(name="LOG_LEVEL", value="debug"),
    EnvItem(secretEnv="proj-secret", vars=["DB_PASS", "API_KEY"]),
    EnvItem(configMap="global-cm"),
    EnvItem(secret="ext-secret"),
    EnvItem(k8s={"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}),
]
env_list, env_from = build_env_items(envs)

results.append(test("plain value in env_list", {"name": "LOG_LEVEL", "value": "debug"} in env_list))
results.append(test("secretEnv → DB_PASS in env_list",
    any(e.get("name") == "DB_PASS" and "secretKeyRef" in e.get("valueFrom", {}) for e in env_list)))
results.append(test("secretEnv → API_KEY in env_list",
    any(e.get("name") == "API_KEY" for e in env_list)))
results.append(test("configMap → envFrom", {"configMapRef": {"name": "global-cm"}} in env_from))
results.append(test("secret → envFrom", {"secretRef": {"name": "ext-secret"}} in env_from))
results.append(test("k8s env in env_list", any(e.get("name") == "POD_IP" for e in env_list)))


# ===========================================================================
# 3. VolumeItem validation
# ===========================================================================
section("3. VolumeItem — Validation")

# Valid: pvc
try:
    v = VolumeItem(name="data", mountPath="/data", pvc="my-claim",
                   readOnly=True, mountPropagation="None", recursiveReadOnly="Enabled")
    results.append(test("pvc volume with mount options",
        v.pvc == "my-claim" and v.readOnly is True and v.mountPropagation == "None"))
except Exception as ex:
    results.append(test("pvc volume with mount options", False, str(ex)))

# Valid: emptyDir
try:
    v = VolumeItem(name="tmp", mountPath="/tmp", emptyDir={})
    results.append(test("emptyDir: {}", v.emptyDir == {}))
except Exception as ex:
    results.append(test("emptyDir: {}", False, str(ex)))

# Valid: emptyDir with options
try:
    v = VolumeItem(name="mem", mountPath="/cache", emptyDir={"medium": "Memory"})
    results.append(test("emptyDir with medium", v.emptyDir["medium"] == "Memory"))
except Exception as ex:
    results.append(test("emptyDir with medium", False, str(ex)))

# Valid: hostPath string shorthand
try:
    v = VolumeItem(name="logs", mountPath="/var/log", hostPath="/var/log/nodes")
    results.append(test("hostPath string shorthand", v.hostPath == "/var/log/nodes"))
except Exception as ex:
    results.append(test("hostPath string shorthand", False, str(ex)))

# Valid: configMap string shorthand
try:
    v = VolumeItem(name="cfg", mountPath="/etc/cfg", configMap="my-cm")
    results.append(test("configMap string shorthand", v.configMap == "my-cm"))
except Exception as ex:
    results.append(test("configMap string shorthand", False, str(ex)))

# Valid: configMap full spec
try:
    v = VolumeItem(name="cfg2", mountPath="/etc/cfg2",
                   configMap={"name": "my-cm", "items": [{"key": "a", "path": "a"}]})
    results.append(test("configMap full spec dict", isinstance(v.configMap, dict)))
except Exception as ex:
    results.append(test("configMap full spec dict", False, str(ex)))

# Valid: secret string shorthand
try:
    v = VolumeItem(name="certs", mountPath="/certs", secret="tls-cert")
    results.append(test("secret string shorthand", v.secret == "tls-cert"))
except Exception as ex:
    results.append(test("secret string shorthand", False, str(ex)))

# Valid: secret full spec
try:
    v = VolumeItem(name="certs2", mountPath="/certs2",
                   secret={"secretName": "tls-cert", "items": [{"key": "tls.crt", "path": "tls.crt"}]})
    results.append(test("secret full spec dict", isinstance(v.secret, dict)))
except Exception as ex:
    results.append(test("secret full spec dict", False, str(ex)))

# Valid: k8s escape hatch
try:
    v = VolumeItem(k8s={
        "volume": {"name": "nfs", "nfs": {"server": "nfs.example.com", "path": "/exports"}},
        "mount": {"mountPath": "/mnt/nfs", "readOnly": True},
    })
    results.append(test("k8s volume", "volume" in v.k8s))
except Exception as ex:
    results.append(test("k8s volume", False, str(ex)))

# Invalid: multiple sources
try:
    v = VolumeItem(name="x", mountPath="/x", pvc="y", emptyDir={})
    results.append(test("multiple sources → should fail", False, "No error raised"))
except Exception:
    results.append(test("multiple sources → should fail", True))

# Invalid: no source
try:
    v = VolumeItem(name="x", mountPath="/x")
    results.append(test("no source → should fail", False, "No error raised"))
except Exception:
    results.append(test("no source → should fail", True))


# ===========================================================================
# 4. build_volume_items
# ===========================================================================
section("4. build_volume_items — Output")

volumes = [
    VolumeItem(name="data", mountPath="/data", pvc="my-pvc", readOnly=True, mountPropagation="None"),
    VolumeItem(name="cache", mountPath="/cache", emptyDir={}),
    VolumeItem(name="logs", mountPath="/var/log", hostPath="/var/log/nodes"),
    VolumeItem(name="cfg", mountPath="/etc/app", configMap="my-cm"),
    VolumeItem(name="certs", mountPath="/certs", secret="tls-secret"),
    VolumeItem(k8s={
        "volume": {"name": "nfs", "nfs": {"server": "nfs.example.com", "path": "/exports"}},
        "mount": {"mountPath": "/mnt/nfs"},
    }),
]
vol_specs, mount_specs = build_volume_items(volumes)

results.append(test("pvc → persistentVolumeClaim",
    any(v.get("persistentVolumeClaim", {}).get("claimName") == "my-pvc" for v in vol_specs)))
results.append(test("pvc mount → readOnly=True",
    any(m.get("name") == "data" and m.get("readOnly") is True for m in mount_specs)))
results.append(test("pvc mount → mountPropagation=None",
    any(m.get("name") == "data" and m.get("mountPropagation") == "None" for m in mount_specs)))
results.append(test("emptyDir → {}",
    any("emptyDir" in v and v["name"] == "cache" for v in vol_specs)))
results.append(test("hostPath string → dict",
    any(v.get("hostPath", {}).get("path") == "/var/log/nodes" for v in vol_specs)))
results.append(test("configMap string → {name: cm}",
    any(v.get("configMap", {}).get("name") == "my-cm" for v in vol_specs)))
results.append(test("secret string → {secretName: ...}",
    any(v.get("secret", {}).get("secretName") == "tls-secret" for v in vol_specs)))
results.append(test("k8s volume preserved",
    any(v.get("name") == "nfs" and "nfs" in v for v in vol_specs)))
results.append(test("k8s mount has name from volume",
    any(m.get("name") == "nfs" for m in mount_specs)))


# ===========================================================================
# 5. ProjectDefinition with pvcs
# ===========================================================================
section("5. ProjectDefinition — pvcs field")

pd = ProjectDefinition(
    project="test-project",
    pvcs=[
        {"name": "shared-storage", "size": "50Gi", "storageClass": "nfs-client"},
        {"name": "db-data", "size": "20Gi"},
    ],
    apps=[],
)
results.append(test("pvcs parsed", len(pd.pvcs) == 2))
results.append(test("pvc name", pd.pvcs[0].name == "shared-storage"))
results.append(test("pvc storageClass", pd.pvcs[0].storageClass == "nfs-client"))
results.append(test("pvc default accessModes", pd.pvcs[1].accessModes == ["ReadWriteOnce"]))


# ===========================================================================
# 6. Full build_values_yaml integration
# ===========================================================================
section("6. build_values_yaml — Integration")

project = ProjectDefinition(
    project="test-proj",
    image_repo="registry.vn/test",
    image_tag="v1.0",
    pvcs=[{"name": "shared-pvc", "size": "10Gi"}],
    apps=[],
)
app = AppConfig(
    name="my-app",
    port=8080,
    envs=[
        EnvItem(name="ENV", value="prod"),
        EnvItem(secretEnv="test-proj-secret", vars=["DB_PASS"]),
        EnvItem(configMap="extra-cfg"),
        EnvItem(secret="extra-sec"),
    ],
    volumes=[
        VolumeItem(name="data", mountPath="/data", pvc="shared-pvc", readOnly=True),
        VolumeItem(name="cache", mountPath="/tmp/cache", emptyDir={}),
    ],
)

values = build_values_yaml(app, project, ({}, []))

# Check env
env = values["deployment"]["env"]
results.append(test("plain ENV value in env", any(e.get("name") == "ENV" and e.get("value") == "prod" for e in env)))
results.append(test("secretEnv DB_PASS in env via secretKeyRef",
    any(e.get("name") == "DB_PASS" and "secretKeyRef" in e.get("valueFrom", {}) for e in env)))

# Check envFrom
env_from = values["deployment"]["envFrom"]
# LOGIC-2 fix: config_pool is empty ({}) so project configmap should NOT be injected
results.append(test("no configmap in envFrom when config_pool empty",
    not any(e.get("configMapRef", {}).get("name") == "test-proj-config" for e in env_from)))
results.append(test("extra configMap in envFrom",
    any(e.get("configMapRef", {}).get("name") == "extra-cfg" for e in env_from)))
results.append(test("extra secret in envFrom",
    any(e.get("secretRef", {}).get("name") == "extra-sec" for e in env_from)))

# Check volumes
vol_specs = values["deployment"]["volumes"]
mount_specs = values["deployment"]["volumeMounts"]
results.append(test("auto /tmp emptyDir volume present",
    any(v.get("name") == "tmp" for v in vol_specs)))
results.append(test("pvc volume ref",
    any(v.get("persistentVolumeClaim", {}).get("claimName") == "shared-pvc" for v in vol_specs)))
results.append(test("pvc mount readOnly=True",
    any(m.get("name") == "data" and m.get("readOnly") is True for m in mount_specs)))
results.append(test("emptyDir cache volume",
    any("emptyDir" in v and v.get("name") == "cache" for v in vol_specs)))

# Check image
results.append(test("image repository", values["image"]["repository"] == "registry.vn/test/my-app"))
results.append(test("image tag", values["image"]["tag"] == "v1.0"))

# ===========================================================================
# 7. BUG-1: env_vars — always inject, even if not in secret_pool
# ===========================================================================
section("7. BUG-1 Fix — env_vars always injected")

project_b1 = ProjectDefinition(project="proj-b1", image_tag="v1", apps=[])
app_b1 = AppConfig(
    name="app-b1",
    port=8080,
    env_vars=["DB_URL", "API_KEY"],  # Neither key is in secret_pool
)
# Pass empty secret_pool — BUG-1 fix: both keys should still be injected
values_b1 = build_values_yaml(app_b1, project_b1, ({}, []))
env_b1 = values_b1["deployment"]["env"]
results.append(test("env_var injected even when not in secret_pool",
    any(e.get("name") == "DB_URL" and "secretKeyRef" in e.get("valueFrom", {}) for e in env_b1)))
results.append(test("env_var API_KEY also injected",
    any(e.get("name") == "API_KEY" and "secretKeyRef" in e.get("valueFrom", {}) for e in env_b1)))

# ===========================================================================
# 8. BUG-2: ingress + service=false → should fail at AppConfig validation
# ===========================================================================
section("8. BUG-2 Fix — ingress + service=false validation")

try:
    bad_app = AppConfig(
        name="bad-app",
        port=8080,
        service=False,
        ingress={"enabled": True, "host": "example.com"},
    )
    results.append(test("ingress + service=false → should fail at model", False, "No error raised"))
except Exception as ex:
    results.append(test("ingress + service=false → should fail at model",
        "ingress.enabled=true requires a Service backend" in str(ex)))

# ===========================================================================
# 9. LOGIC-2: project ConfigMap only injected when config_pool has data
# ===========================================================================
section("9. LOGIC-2 Fix — conditional project ConfigMap injection")

project_l2 = ProjectDefinition(project="proj-l2", image_tag="v1", apps=[])
app_l2 = AppConfig(name="app-l2", port=8080)

# No config_pool → no configmap
values_no_cm = build_values_yaml(app_l2, project_l2, ({}, []))
results.append(test("no configmap in envFrom when config_pool={}",
    not any("configMapRef" in e for e in values_no_cm["deployment"]["envFrom"])))

# With config_pool → configmap IS injected
values_with_cm = build_values_yaml(app_l2, project_l2, ({"KEY": "val"}, []))
results.append(test("configmap in envFrom when config_pool has data",
    any(e.get("configMapRef", {}).get("name") == "proj-l2-config"
        for e in values_with_cm["deployment"]["envFrom"])))

# ===========================================================================
# 10. LOGIC-3: Nginx annotations merged in generator
# ===========================================================================
section("10. LOGIC-3 Fix — Nginx annotations merge")

project_l3 = ProjectDefinition(project="proj-l3", image_tag="v1", apps=[])
app_nginx = AppConfig(
    name="app-l3",
    port=8080,
    ingress={
        "enabled": True,
        "host": "test.example.com",
        "className": "nginx",
        "annotations": {"nginx.ingress.kubernetes.io/ssl-redirect": "true"},  # Override default
    },
)
values_l3 = build_values_yaml(app_nginx, project_l3, ({}, []))
annots = values_l3["ingress"]["annotations"]
results.append(test("user override for ssl-redirect wins",
    annots.get("nginx.ingress.kubernetes.io/ssl-redirect") == "true"))
results.append(test("proxy-body-size default still present",
    "nginx.ingress.kubernetes.io/proxy-body-size" in annots))

# Non-nginx: no default annotations injected
app_traefik = AppConfig(
    name="traefik-app",
    port=8080,
    ingress={"enabled": True, "host": "t.example.com", "className": "traefik"},
)
values_traefik = build_values_yaml(app_traefik, project_l3, ({}, []))
annots_traefik = values_traefik["ingress"].get("annotations", {})
results.append(test("non-nginx: no default nginx annotations",
    "nginx.ingress.kubernetes.io/ssl-redirect" not in annots_traefik))

# ===========================================================================
# 11. IMPROVE-2: K8s name validation
# ===========================================================================
section("11. IMPROVE-2 — K8s name validation")

# Valid name
try:
    _validate_k8s_name("my-app")
    results.append(test("valid k8s name 'my-app'", True))
except Exception as ex:
    results.append(test("valid k8s name 'my-app'", False, str(ex)))

# Invalid: uppercase
try:
    _validate_k8s_name("MyApp")
    results.append(test("uppercase name → should fail", False, "No error raised"))
except Exception:
    results.append(test("uppercase name → should fail", True))

# Invalid: underscore
try:
    _validate_k8s_name("my_app")
    results.append(test("underscore name → should fail", False, "No error raised"))
except Exception:
    results.append(test("underscore name → should fail", True))

# Invalid: starts with dash
try:
    _validate_k8s_name("-myapp")
    results.append(test("leading dash → should fail", False, "No error raised"))
except Exception:
    results.append(test("leading dash → should fail", True))

# Invalid: too long
try:
    _validate_k8s_name("a" * 64)
    results.append(test("64-char name → should fail", False, "No error raised"))
except Exception:
    results.append(test("64-char name → should fail", True))

# App validation catches bad name
try:
    AppConfig(name="Bad_Name", port=8080)
    results.append(test("AppConfig with bad name → should fail", False, "No error raised"))
except Exception:
    results.append(test("AppConfig with bad name → should fail", True))

# ===========================================================================
# 12. LOGIC-5: parse_env_file quote handling
# ===========================================================================
section("12. LOGIC-5 Fix — parse_env_file quote handling")

import tempfile, os
with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
    f.write('DB_URL="postgres://user:pass@host:5432/db"\n')
    f.write("API_SECRET='my-secret-value'\n")
    f.write('PLAIN=hello=world\n')
    f.write('VAULT_KEY=${vault:secret/data/key}\n')
    f.write('# comment line\n')
    tmp_path = f.name

from pathlib import Path as _Path
cfg, secrets = parse_env_file(_Path(tmp_path))
os.unlink(tmp_path)

results.append(test('double-quoted value stripped correctly',
    cfg.get('DB_URL') == 'postgres://user:pass@host:5432/db'))
results.append(test("single-quoted value stripped correctly",
    cfg.get('API_SECRET') == 'my-secret-value'))
results.append(test('value with = inside parsed correctly',
    cfg.get('PLAIN') == 'hello=world'))
results.append(test('vault placeholder detected as secret',
    'VAULT_KEY' in secrets))
results.append(test('comment line ignored', 'DB_URL' in cfg and len(cfg) == 3))


# ===========================================================================
# 13. BUG-1: Image URI with embedded tag must not double-stack tags
# ===========================================================================
section("13. BUG-1 Fix — Image URI with embedded tag")

project_img = ProjectDefinition(project="proj-img", image_tag="latest", apps=[])

# Case 1: Simple image:tag (e.g. redis:7.2-alpine)
app_img1 = AppConfig(name="my-cache", image="redis:7.2-alpine")
v1 = build_values_yaml(app_img1, project_img, ({}, []))
results.append(test("simple image:tag — repository is 'redis' without tag",
    v1["image"]["repository"] == "redis"))
results.append(test("simple image:tag — tag is '7.2-alpine'",
    v1["image"]["tag"] == "7.2-alpine"))

# Case 2: Multi-component path with tag (e.g. myrepo/myapp:v1.2.3)
app_img2 = AppConfig(name="my-app", image="registry.example.com/myteam/myapp:v1.2.3")
v2 = build_values_yaml(app_img2, project_img, ({}, []))
results.append(test("full path image:tag — repository correct",
    v2["image"]["repository"] == "registry.example.com/myteam/myapp"))
results.append(test("full path image:tag — tag is 'v1.2.3'",
    v2["image"]["tag"] == "v1.2.3"))

# Case 3: CLI --image-tag overrides embedded tag
app_img3 = AppConfig(name="my-app2", image="redis:7.0")
v3 = build_values_yaml(app_img3, project_img, ({}, []), image_tag="8.0-override")
results.append(test("CLI --image-tag overrides embedded tag",
    v3["image"]["tag"] == "8.0-override"))

# Case 4: Plain image without tag — uses project image_tag
app_img4 = AppConfig(name="my-svc", image="my-registry/my-svc")
v4 = build_values_yaml(app_img4, project_img, ({}, []))
results.append(test("plain image (no tag) — uses project image_tag 'latest'",
    v4["image"]["tag"] == "latest"))


# ===========================================================================
# 14. BUG-2: Auto /tmp mount skipped when user explicitly mounts /tmp
# ===========================================================================
section("14. BUG-2 Fix — auto_mount_tmp dedup")

project_tmp = ProjectDefinition(project="proj-tmp", image_tag="v1", apps=[])

# Case 1: User has /tmp volume → auto-mount should be suppressed
app_tmp = AppConfig(
    name="app-tmp",
    port=8080,
    volumes=[
        VolumeItem(name="ramdisk", mountPath="/tmp", emptyDir={"medium": "Memory"}),
    ],
)
v_tmp = build_values_yaml(app_tmp, project_tmp, ({}, []))
tmp_vols = [v for v in v_tmp["deployment"]["volumes"] if v.get("mountPath") == "/tmp"
            or v.get("name") == "tmp"]
tmp_mounts = [m for m in v_tmp["deployment"]["volumeMounts"] if m.get("mountPath") == "/tmp"]
results.append(test("user-defined /tmp — no auto-generated 'tmp' volume added",
    sum(1 for v in v_tmp["deployment"]["volumes"] if v.get("name") in ("tmp", "ramdisk")) == 1))
results.append(test("user-defined /tmp — only one mount at /tmp",
    len(tmp_mounts) == 1))
results.append(test("user-defined /tmp — mount uses user's name 'ramdisk'",
    tmp_mounts[0]["name"] == "ramdisk" if tmp_mounts else False))

# Case 2: No user /tmp → auto-mount IS added
app_notmp = AppConfig(name="app-notmp", port=8080)
v_notmp = build_values_yaml(app_notmp, project_tmp, ({}, []))
results.append(test("no user /tmp — auto 'tmp' volume is added",
    any(v.get("name") == "tmp" and "emptyDir" in v for v in v_notmp["deployment"]["volumes"])))


# ===========================================================================
# 15. BUG-3: Port name collision — shorthand 'port:' + ports with name='http'
# ===========================================================================
section("15. BUG-3 Fix — Port name uniqueness")

project_port = ProjectDefinition(project="proj-port", image_tag="v1", apps=[])

# Case: shorthand port=8080 + user has ports=[{name: http, port: 9000}]
app_port_coll = AppConfig(
    name="app-port",
    port=8080,  # shorthand
    ports=[
        ServicePort(name="http", port=9000),    # already uses 'http'
        ServicePort(name="metrics", port=9100),
    ],
)
v_port = build_values_yaml(app_port_coll, project_port, ({}, []))
deploy_ports = v_port["deployment"]["ports"]
port_names = [p["name"] for p in deploy_ports]
results.append(test("port names are all unique (no duplicates)",
    len(port_names) == len(set(port_names))))
results.append(test("shorthand port 8080 inserted with name 'primary' (not 'http')",
    any(p["name"] == "primary" and p["containerPort"] == 8080 for p in deploy_ports)))
results.append(test("original http port 9000 still present",
    any(p["name"] == "http" and p["containerPort"] == 9000 for p in deploy_ports)))


# ===========================================================================
# 16. LOGIC-1: storageClass="" must be rendered (not silently dropped)
# ===========================================================================
section("16. LOGIC-1 Fix — storageClass empty string preserved")

# Empty string storageClass: K8s semantics = use a PV with no StorageClass
pvc_empty_class = ProjectPVC(name="no-class-pvc", size="5Gi", storageClass="")
yaml_out = build_project_pvcs_yaml([pvc_empty_class])
results.append(test("storageClass='' renders storageClassName key in YAML",
    "storageClassName" in yaml_out))
results.append(test("storageClassName value is empty string",
    'storageClassName: ""' in yaml_out or "storageClassName: ''" in yaml_out))

# None (not set) → storageClassName should NOT be present
pvc_no_class = ProjectPVC(name="default-class-pvc", size="5Gi")
yaml_no = build_project_pvcs_yaml([pvc_no_class])
results.append(test("storageClass=None → storageClassName absent from YAML",
    "storageClassName" not in yaml_no))


# ===========================================================================
# 17. LOGIC-2: project-shared not created when only secret_keys exist (no config_pool)
# ===========================================================================
section("17. LOGIC-2 Fix — No empty ConfigMap generation logic")

# Simulate: file has ONLY secret placeholders (config_pool={}, secret_keys=['X'])
# build_values_yaml should NOT inject project configMapRef when config_pool empty
project_l2b = ProjectDefinition(project="proj-l2b", image_tag="v1", apps=[])
app_l2b = AppConfig(name="app-l2b", port=8080)

# Only secrets, no config data
values_secrets_only = build_values_yaml(app_l2b, project_l2b, ({}, ["VAULT_KEY"]))
results.append(test("secretsOnly: no configMapRef in envFrom when config_pool={}",
    not any("configMapRef" in e for e in values_secrets_only["deployment"]["envFrom"])))

# Both config + secrets
values_both = build_values_yaml(app_l2b, project_l2b, ({"APP_ENV": "prod"}, ["VAULT_KEY"]))
results.append(test("config+secrets: configMapRef IS in envFrom when config_pool has data",
    any(e.get("configMapRef", {}).get("name") == "proj-l2b-config"
        for e in values_both["deployment"]["envFrom"])))


# ===========================================================================
# Summary
# ===========================================================================
section("Summary")
passed = sum(results)
total = len(results)
print(f"\n  {passed}/{total} tests passed")
if passed == total:
    print("  🎉 All tests passed!")
else:
    print(f"  ⚠️  {total - passed} test(s) failed.")
sys.exit(0 if passed == total else 1)
