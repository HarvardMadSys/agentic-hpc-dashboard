# ebpfm collector image, built on the project's Rocky 8.10 base.
#
# The eBPF tier needs python3-bcc (which JIT-compiles its BPF C against the
# running kernel's BTF/headers at load time), bpftool, iproute (ss) and jq. The
# unprivileged node tier shells out to sysstat (mpstat/iostat/sar), nfs-utils
# (nfsiostat) and procps-ng (free/uptime/w); missing ones only blank individual
# sections, but a login-node image should carry them.
#
# BASE_IMAGE is set by CI to the base its ref builds on (collector-image.yml:
# base:latest for a tag, base:edge for main, base:<branch> for a branch that
# changed docker/base, else base:edge); the default is the newest release's base, which the first vX.Y.Z tag publishes. Until then, build
# locally with --build-arg BASE_IMAGE=ghcr.io/harvardmadsys/agentic-hpc-dashboard/base:edge
# (main's newest).
ARG BASE_IMAGE=ghcr.io/harvardmadsys/agentic-hpc-dashboard/base:latest
FROM ${BASE_IMAGE}

RUN dnf -y install \
        python3 \
        python3-bcc \
        bpftool \
        iproute \
        jq \
        sysstat \
        nfs-utils \
        procps-ng && \
    dnf clean all && \
    rm -rf /var/cache/yum /var/cache/dnf

# The collector bundle is standalone (ebpf_trace.py + node_snapshot.py + lib/),
# so copying collector/ is sufficient — no repo context is required at runtime.
COPY collector/ /opt/ebpfm/
COPY docker/collector/entrypoint.sh /usr/local/bin/entrypoint.sh
# EBPFM_VERSION, when the build passes one, is the collector's version
# (`ebpfm version`, and collector_version in the tracer's meta record); without
# it the collector names itself by a hash of its code, as the image has no .git.
# `ebpfm` on PATH, for `docker exec <container> ebpfm status`.
ARG EBPFM_VERSION=
RUN chmod +x /usr/local/bin/entrypoint.sh /opt/ebpfm/ebpfm.sh && \
    rm -rf /opt/ebpfm/tests && \
    ln -s /opt/ebpfm/ebpfm.sh /usr/local/bin/ebpfm && \
    if [ -n "$EBPFM_VERSION" ]; then echo "$EBPFM_VERSION" > /opt/ebpfm/VERSION; fi

# The image runs the eBPF tier alone, so `ebpfm check` and `ebpfm status` ask
# only about that tier.
ENV EBPFM_OUTPUT_DIR=/var/log/ebpfm \
    EBPFM_NODE_OUTPUT_DIR=/var/log/ebpfm-login \
    EBPFM_TIER=ebpf

VOLUME ["/var/log/ebpfm", "/var/log/ebpfm-login"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["collector"]
