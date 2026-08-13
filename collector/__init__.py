"""Collector package: transport client, entity collectors and the data model.

Modules:
    client       -- PrismClient transport (session, auth, retries, pagination)
                    and MockClient (fixture-backed drop-in for offline runs).
    metric_names -- one place to adjust API metric field names per PC version.
    model        -- internal dataclasses (Cluster, Host, VM, Summary).
    clusters     -- cluster inventory + stats collection (with PC exclusion).
    hosts        -- host inventory + stats collection.
    vms          -- VM inventory + stats + guest storage collection.
    efficiency   -- v3 groups API efficiency states, alerts and runway KPIs.
"""
