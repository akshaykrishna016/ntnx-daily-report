"""Metric-name probe for the first live run against a Prism Central.

The design document (sections 5.3 and 13) requires a small probe that hits each
stats endpoint for one entity and dumps the available field names, so the
constants in ``collector/metric_names.py`` can be confirmed or corrected in one
place before the real report is scheduled.

This tool is NOT part of the scheduled run. Run it once by hand:

    PC_PASSWORD=... python3 probe_metrics.py

It:
  1. lists clusters and prints each cluster's function list (so you can confirm
     which entity is the Prism Central self-cluster to exclude);
  2. picks the first AOS cluster, its first host, and the first VM, calls each
     stats endpoint, and prints the metric keys actually present in the
     response.

Nothing is emailed and no files are written.
"""

import json
import os
import sys

import yaml

from collector import metric_names
from collector.client import PrismClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_config():
    """Load config.yaml from the project directory."""
    with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as h:
        return yaml.safe_load(h)


def _print_keys(label, payload):
    """Print the metric keys found in a stats payload's first data entry."""
    data = payload.get("data") or []
    if not data:
        print("  {label}: no data returned".format(label=label))
        return
    entity = data[0] or {}
    keys = sorted(entity.keys())
    print("  {label}: {keys}".format(label=label, keys=keys))


def main():
    """Probe the live Prism Central and print discovered field names."""
    config = _load_config()
    password = os.environ.get("PC_PASSWORD")
    if not password:
        raise SystemExit("Set PC_PASSWORD before running the probe.")

    pc = config["prism_central"]
    client = PrismClient(
        host=pc["host"],
        port=pc["port"],
        username=pc["username"],
        password=password,
        verify_ssl=pc.get("verify_ssl", False),
    )

    print("== Clusters and their function lists ==")
    clusters = client.paginate_v4("/api/clustermgmt/v4.0/config/clusters")
    aos_cluster = None
    for raw in clusters:
        functions = (raw.get("config") or {}).get(
            metric_names.CLUSTER_FUNCTION_FIELD
        )
        print("  {name}: {functions}".format(
            name=raw.get("name"), functions=functions))
        if aos_cluster is None and metric_names.CLUSTER_FUNCTION_AOS in (
            functions or []
        ):
            aos_cluster = raw

    if aos_cluster is None:
        print("No AOS cluster found; dumping first cluster config for review:")
        print(json.dumps(clusters[0] if clusters else {}, indent=2))
        return 0

    cid = aos_cluster["extId"]
    print("\n== Cluster stats keys (cluster {cid}) ==".format(cid=cid))
    _print_keys(
        "cluster",
        client.get_json(
            "/api/clustermgmt/v4.0/stats/clusters/{cid}".format(cid=cid)
        ),
    )

    hosts = client.paginate_v4(
        "/api/clustermgmt/v4.0/config/clusters/{cid}/hosts".format(cid=cid)
    )
    if hosts:
        hid = hosts[0]["extId"]
        print("\n== Host stats keys (host {hid}) ==".format(hid=hid))
        _print_keys(
            "host",
            client.get_json(
                "/api/clustermgmt/v4.0/stats/clusters/{cid}/hosts/{hid}".format(
                    cid=cid, hid=hid
                )
            ),
        )

    vms = client.paginate_v4("/api/vmm/v4.0/ahv/config/vms")
    if vms:
        vid = vms[0]["extId"]
        print("\n== VM stats keys (vm {vid}) ==".format(vid=vid))
        _print_keys(
            "vm",
            client.get_json(
                "/api/vmm/v4.0/ahv/stats/vms/{vid}".format(vid=vid)
            ),
        )

    print(
        "\nCompare the keys above with collector/metric_names.py and adjust "
        "the constants there if they differ."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
