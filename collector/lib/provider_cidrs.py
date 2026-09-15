#!/usr/bin/env python3
"""Provider IP-range table for the nettcp collector — maps a remote IP to a cloud/

VENDORED COPY — see ../VENDORED.md. Upstream is collect/nettcp/provider_cidrs.py; keep the two in sync.
`ebpfm.sh check` diffs them whenever it can see the repo.
AI-inference provider so login-node egress can be bucketed ("is the brain in the
cloud?").

Two layers:
  * STATIC — published ranges hard-coded below, with source URLs + retrieval date.
  * RESOLVED — resolved_cidrs.json next to this file, populated at deploy time by
    `deploy_monitor.sh refresh-cidrs` (DNS of api.*.com + AWS/GCP/GitHub range
    downloads). Absent-file is fine; the static table still classifies the reliable
    Anthropic/GitHub buckets.

Honesty: OpenAI/Azure are Cloudflare/region-fronted and DRIFT — treat those buckets
as weak upper bounds. Anthropic (owned AS399358 /23) and GitHub (published meta) are
reliable. Classification prefers the most specific match, provider priority
anthropic > openai > azure > github > aws > gcp on ties.

Retrieved 2026-07-06 on holylogin07.
"""
import ipaddress
import json
import os

# (provider, cidr, source)
STATIC = [
    # Anthropic — https://platform.claude.com/docs/en/api/ip-addresses (reliable)
    ("anthropic", "160.79.104.0/23", "anthropic-inbound"),
    ("anthropic", "160.79.104.0/21", "anthropic-outbound"),
    ("anthropic", "2607:6bc0::/48", "anthropic-ipv6"),
    # OpenAI — api.openai.com is Cloudflare-fronted (weak) + chatgpt-actions.json /28s
    ("openai", "162.159.140.0/24", "cloudflare-openai"),
    ("openai", "172.66.0.0/24", "cloudflare-openai"),
    ("openai", "104.210.139.192/28", "chatgpt-actions"),
    ("openai", "104.210.139.224/28", "chatgpt-actions"),
    ("openai", "13.65.138.96/28", "chatgpt-actions"),
    ("openai", "13.65.138.112/28", "chatgpt-actions"),
    # Azure OpenAI regional endpoints (resolved 2026-07-06; DRIFT — refresh re-resolves)
    ("azure", "20.232.91.0/24", "eastus-cognitive"),
    ("azure", "20.59.86.0/24", "westus-cognitive"),
    ("azure", "20.119.156.0/24", "eastus2-openai"),
    # GitHub — https://api.github.com/meta (reliable)
    ("github", "192.30.252.0/22", "github-meta"),
    ("github", "185.199.108.0/22", "github-meta"),
    ("github", "140.82.112.0/20", "github-meta"),
    ("github", "143.55.64.0/20", "github-meta"),
    ("github", "2a0a:a440::/29", "github-meta"),
    ("github", "2606:50c0::/32", "github-meta"),
]

PRIORITY = {"anthropic": 0, "openai": 1, "azure": 2, "github": 3, "aws": 4, "gcp": 5}


class CidrIndex:
    """Longest-prefix, priority-broken classifier. v4 networks bucketed by first
    octet for speed (matters when refresh loads ~8k AWS prefixes)."""

    def __init__(self, entries):
        self.v4 = {}          # first octet -> [(net, provider)]
        self.v6 = []          # [(net, provider)]
        for provider, cidr, _src in entries:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                continue
            if net.version == 4:
                first = int(net.network_address) >> 24 & 0xFF
                self.v4.setdefault(first, []).append((net, provider))
            else:
                self.v6.append((net, provider))

    def classify(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if addr.version == 4:
            cands = self.v4.get(int(addr) >> 24 & 0xFF, [])
        else:
            cands = self.v6
        best = None  # (prefixlen, -priority, provider)
        for net, provider in cands:
            if addr in net:
                key = (net.prefixlen, -PRIORITY.get(provider, 9))
                if best is None or key > best[0]:
                    best = (key, provider)
        return best[1] if best else None


def load_index():
    entries = list(STATIC)
    resolved = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resolved_cidrs.json")
    try:
        for provider, cidrs in json.load(open(resolved)).items():
            for c in cidrs:
                entries.append((provider, c, "resolved"))
    except OSError:
        pass
    return CidrIndex(entries)


if __name__ == "__main__":
    idx = load_index()
    for ip in ("160.79.104.10", "140.82.114.6", "20.232.91.180", "8.8.8.8"):
        print(ip, "->", idx.classify(ip))
