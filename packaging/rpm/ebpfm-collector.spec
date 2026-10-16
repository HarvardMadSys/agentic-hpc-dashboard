# RPM spec for the ebpfm login-node collector.
#
# Packages the standalone collector bundle (ebpf_trace.py, node_snapshot.py,
# lib/, ebpfm.sh) into /usr/lib/ebpfm, installs a systemd unit with the paths
# pre-substituted, and symlinks the entrypoint to /usr/bin/ebpfm.
#
# Version/Release are injected by the release workflow via rpmbuild --define:
#   semver release tag v1.2.3 -> Version 1.2.3, Release 1
#   feature branch build      -> Version 0.0.0, Release <YYYYMMDD>.<shortsha>
# so a branch RPM sorts BELOW any real release and is self-identifying.

%global ebpfm_libdir %{_prefix}/lib/ebpfm
# Output lands on /scratch, not /var/log: the collector's JSONL is unbounded
# (one record per process exit, highest exactly when the node is busy) and these
# login nodes have a SMALL root disk. /scratch is the large, root-writable local
# volume. It is NOT owned as a %dir here -- /scratch may be a separate mount that
# is not present at install time, and owning a dir under it would either fail or
# strand a directory on the root disk. The collector creates it at runtime
# (os.makedirs/exist_ok), and the unit's RequiresMountsFor=/scratch makes systemd
# wait for the mount first.
%global ebpfm_outputdir /scratch/ebpfm

Name:           ebpfm-collector
Version:        %{?_ebpfm_version}%{!?_ebpfm_version:0.0.0}
Release:        %{?_ebpfm_release}%{!?_ebpfm_release:1}%{?dist}
Summary:        eBPF login-node collector for coding-agent activity (ebpfm)

License:        Proprietary
URL:            https://github.com/fasrc/agentic-hpc-dashboard
BuildArch:      noarch

# Runtime dependencies. The eBPF tier needs python3-bcc + bpftool; ss comes from
# iproute; the node tier shells out to sysstat/nfs-utils/procps-ng and jq.
#
# python3-bcc lives in EPEL and pulls -devel packages that live in PowerTools
# (CRB on EL8). Depending on epel-release makes dnf pull the EPEL repo
# definition in as part of this install; PowerTools ships in the base repo set
# but is disabled by default, so %post enables it. Together these replace the
# repo/dependency half of the old `bootstrap` command: `dnf install` now
# resolves everything on its own.
Requires:       epel-release
Requires:       python3
Requires:       python3-bcc
Requires:       bpftool
Requires:       iproute
Requires:       jq
Requires:       sysstat
Requires:       nfs-utils
Requires:       procps-ng
%{?systemd_requires}
BuildRequires:  systemd-rpm-macros

%description
ebpfm collects coding-agent activity on HPC login nodes in two tiers: an
unprivileged node-health tier and a root eBPF tier that records per-process
exits, argv, cwd, all-user I/O, D-state dwell, network bytes and TCP records.

This package installs the standalone collector bundle under %{ebpfm_libdir},
a systemd unit (ebpfm.service) that runs the eBPF tier, and the `ebpfm`
command (a symlink to ebpfm.sh) for check/status/tail.

Package dependencies (python3-bcc from EPEL, bpftool, iproute, jq, sysstat,
nfs-utils, procps-ng) are resolved by dnf. The two required kernel sysctls
(kernel.sched_schedstats, kernel.task_delayacct) ship as a sysctl.d drop-in and
are applied on install and on every boot -- no separate bootstrap step. Run
`ebpfm check` to confirm 0 FAIL, then `systemctl enable --now ebpfm`.

The unit is NOT enabled by default.

%prep
# The release workflow drops the bundle tree into the build root directly; no
# upstream tarball unpack is required.

%build
# Nothing to compile — the collector is Python + shell.

%install
rm -rf %{buildroot}

# Collector bundle -> /usr/lib/ebpfm
install -d -m 0755 %{buildroot}%{ebpfm_libdir}
install -d -m 0755 %{buildroot}%{ebpfm_libdir}/lib
install -m 0755 %{_sourcedir}/collector/ebpf_trace.py    %{buildroot}%{ebpfm_libdir}/ebpf_trace.py
install -m 0755 %{_sourcedir}/collector/node_snapshot.py %{buildroot}%{ebpfm_libdir}/node_snapshot.py
install -m 0755 %{_sourcedir}/collector/ebpfm.sh         %{buildroot}%{ebpfm_libdir}/ebpfm.sh
install -m 0644 %{_sourcedir}/collector/lib/*.py         %{buildroot}%{ebpfm_libdir}/lib/
# Vendoring manifest, used by `ebpfm check`.
install -m 0644 %{_sourcedir}/collector/.vendored.sha256 %{buildroot}%{ebpfm_libdir}/.vendored.sha256

# Entrypoint symlink -> /usr/bin/ebpfm
install -d -m 0755 %{buildroot}%{_bindir}
ln -s %{ebpfm_libdir}/ebpfm.sh %{buildroot}%{_bindir}/ebpfm

# systemd unit, with @EBPFM_DIR@/@EBPFM_OUTPUT_DIR@ pre-substituted so the unit
# is usable without running `ebpfm install-unit`.
install -d -m 0755 %{buildroot}%{_unitdir}
sed -e "s#@EBPFM_DIR@#%{ebpfm_libdir}#g" \
    -e "s#@EBPFM_OUTPUT_DIR@#%{ebpfm_outputdir}#g" \
    %{_sourcedir}/collector/ebpfm.service > %{buildroot}%{_unitdir}/ebpfm.service

# Output directory is NOT created here: it lives on /scratch (see ebpfm_outputdir
# above), which may be an unmounted separate volume at install time. The
# collector creates it at runtime; the unit waits for the mount.

# Required sysctls, shipped as a drop-in so systemd-sysctl applies them on boot
# and the file is owned/verifiable by the package. %post applies it immediately.
install -d -m 0755 %{buildroot}%{_sysctldir}
install -m 0644 %{_sourcedir}/packaging/99-ebpfm.conf %{buildroot}%{_sysctldir}/99-ebpfm.conf

# Service environment file. Admin-editable, so it is %config(noreplace) below.
install -d -m 0755 %{buildroot}%{_sysconfdir}/default
install -m 0644 %{_sourcedir}/packaging/ebpfm.default %{buildroot}%{_sysconfdir}/default/ebpfm

%files
%dir %{ebpfm_libdir}
%dir %{ebpfm_libdir}/lib
%{ebpfm_libdir}/ebpf_trace.py
%{ebpfm_libdir}/node_snapshot.py
%{ebpfm_libdir}/ebpfm.sh
%{ebpfm_libdir}/lib/*.py
%{ebpfm_libdir}/.vendored.sha256
%{_bindir}/ebpfm
%{_unitdir}/ebpfm.service
# Vendor sysctl drop-in in /usr/lib/sysctl.d (not %config): admins override in
# /etc/sysctl.d, which takes precedence, so the shipped file stays canonical.
%{_sysctldir}/99-ebpfm.conf
# Service env file in /etc: admin-editable, preserve local changes on upgrade.
%config(noreplace) %{_sysconfdir}/default/ebpfm

%post
%systemd_post ebpfm.service
# Enable PowerTools/CRB: python3-bcc's -devel dependencies live there, and it is
# disabled by default on EL8. Idempotent; name differs across EL8 minor repos
# so try both. Best-effort -- a FAIL here must not abort the transaction.
if command -v dnf >/dev/null 2>&1; then
    dnf config-manager --set-enabled powertools >/dev/null 2>&1 \
        || dnf config-manager --set-enabled PowerTools >/dev/null 2>&1 \
        || dnf config-manager --set-enabled crb >/dev/null 2>&1 || :
fi
# Apply the required sysctls now so the collector works without a reboot.
# systemd-sysctl re-applies 99-ebpfm.conf on every boot thereafter.
if [ -x /usr/lib/systemd/systemd-sysctl ]; then
    /usr/lib/systemd/systemd-sysctl %{_sysctldir}/99-ebpfm.conf >/dev/null 2>&1 || :
else
    sysctl -p %{_sysctldir}/99-ebpfm.conf >/dev/null 2>&1 || :
fi

%preun
%systemd_preun ebpfm.service

%postun
%systemd_postun_with_restart ebpfm.service

%changelog
* Thu Jan 01 2026 agentic-hpc-dashboard maintainers <noreply@github.com> - 0.0.0-1
- Initial RPM packaging of the ebpfm collector. Version/Release are set at
  build time by the release workflow; this entry is a placeholder.
