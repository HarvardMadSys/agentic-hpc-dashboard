"""The sacct tier: fold a raw ``sacct`` export into one row per job, then roll it up.

This absorbs two retired modules -- ``archive/reduce_sacct.py`` (the step-row fold)
and the sacct half of ``archive/rc_real.py`` (``rollup_sacct``) -- and fixes four
defects they carried.  Stdlib only: the feed is a *directory* of multi-GB day-files
and everything here streams, so pandas is neither needed nor affordable.

The feed is whatever ``collect/slurm_query.bash`` wrote: per-day files named
``SlurmData_<start>_<stop>-orig.csv``, each a ``____``-delimited
``sacct --parsable2 -D -a`` export carrying a **job** row plus one row per **step**.
The request lives on the job row; the usage (``MaxRSS``, ``TotalCPU``, the disk and
TRES counters) lives on the step rows.  Folding is therefore mandatory, not an
optimisation.

What changed against the archived pair
--------------------------------------

1. **A missing column is now reported, not silently emptied.**  ``reduce_sacct.KEEP``
   listed ``Account, QOS, JobName, Eligible, SubmitLine, Group, UID``; the configured
   ``SACCT_FORMAT`` contains none of the seven, and ``reduce()`` line 76 emitted ``''``
   for each of them.  Downstream that is indistinguishable from "the job had no
   SubmitLine", so the agent attribution quietly lost its command-line half and the
   repeated-submission panel quietly went empty.  Here a column absent from the export
   is absent from the reduced CSV too, is listed in ``missing``, and any panel that
   needed it says so (see ``NOTICE_RETRY``).

2. **Columns the format does carry are no longer thrown away.**  The disk/TRES/energy
   block (``AveDiskRead`` .. ``Comment``) is kept and folded, which is what the I/O
   panel reads.

3. **One row per ``JobID``, with its requeue generations merged rather than dropped.**
   ``sacct -D`` emits one record per *run*, so a preempted-and-requeued job arrives
   several times; ``--starttime=D --endtime=D+1`` additionally emits a run spanning
   midnight in *both* day-files.  The counting grain is one row per job -- a job
   preempted three times is still one job -- while consumed quantities (elapsed, CPU,
   disk, energy) are **summed** across generations, high-water marks take the max, and
   identity/terminal fields come from the last generation.  The midnight case is the
   opposite problem and is told apart by ``Start``: same ``JobID`` *and* same ``Start``
   is one generation observed twice, so it is de-duplicated, never summed.  See
   ``merge_generations``.

4. **The cache key is a fingerprint, not an mtime.**  ``build_dashboard_data.py:103``
   rebuilt only when ``getmtime(cache) < getmtime(src)``.  The observed failure:
   ``archive/.cache/`` holds a cache reduced from a 78-column export, mtime Sep 11,
   sitting beside the 68-column raw file it is supposed to describe, mtime Sep 7 --
   newer cache, older input, so the staleness test says "fresh" forever.  The key now
   covers every input's size, mtime **and header hash**, so a re-pull with a different
   ``--format`` can never be served from an old reduction.

5. **``find_sacct_csv`` returns the newest day-file.**  ``rc_real.py:95-98`` returned
   the first element of a sorted glob, i.e. the *oldest*, contradicting its docstring.

Grain vocabulary follows the repo rule: **job** = array-expanded (one row per
``JobID``), **submission** = array-collapsed (``array_job_id``).  Every agent-vs-other
statement is reported at all four units -- job, submission, user, work_dir -- because
raw counts are dominated by a few mega-array campaigns.

Measured on ``SlurmData_2026-06-01_2026-06-02-orig.csv`` (1.18 GB, 68 columns)
-----------------------------------------------------------------------------
1 275 398 data lines -> 435 211 ``sacct -D`` job records -> **424 941 rows**, one per
JobID, in ~18 s at ~340 MB RSS; the roll-up over the reduced CSV is ~7 s.  Seven
columns are absent (``Account, QOS, JobName, Eligible, SubmitLine, Group, UID``).  The
folded disk/TRES columns land at **87.76%** coverage -- exactly ``MaxRSS``'s, as
expected for step-row fields -- while two populated columns are degenerate on this
cluster: ``MaxVMSize`` is 0 for every job and ``ConsumedEnergyRaw`` is 0 for all of
the 99.9% that carry it (energy accounting is off), which is why ``io_jobs`` reports
``energy_nonzero_jobs`` and excludes energy from ``coverage_pct``.

Every one of the 10 270 duplicate records in that file is **within** one day-file and
has its own ``Start``: they are requeue/preemption generations, not the
midnight-spanning copies the de-duplication was first written for.  8 477 jobs (2.0%)
carry 18 747 generations between them.  Merging rather than discarding them is
conservative in the accounting sense -- summed ``ElapsedRaw`` is 1 776 909 507 s and
summed ``CPUTimeRAW`` 17 124 402 174 s, both **exactly** equal to an independent
``awk`` total over all 435 211 raw job records, and ``dropped_elapsed_s`` is 0.  The
retired keep-the-longest rule lost 14 286 146 elapsed-seconds and 124 336 309
CPU-seconds, understating whole-file core-hours by 0.56% and GPU-hours by 0.81%.
"""
import collections
import csv
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys

csv.field_size_limit(10 ** 7)          # WorkDir/SubmitLine/NodeList can be long

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(HERE)

DELIM = "____"
CACHE_VERSION = 1                      # layout of the .meta.json sidecar
REDUCER_VERSION = 2                    # bump when the fold changes a written value
#   1 -> 2: one row per JobID now MERGES requeue generations (sums the consumed
#           columns) instead of keeping only the longest-ElapsedRaw record, so every
#           cache built by version 1 understates elapsed/CPU/disk and must be rebuilt.

# ---------------------------------------------------------------- columns
# Absent -> the export is unusable for this tier, so raise.
KEEP_CORE = ['JobID', 'User', 'State', 'Submit', 'Start', 'End', 'ElapsedRaw', 'NCPUS',
             'Partition', 'WorkDir']
# Absent -> recorded in `missing`; the panels that need one degrade and say so.
KEEP_OPT = ['Account', 'QOS', 'JobName', 'Eligible', 'SubmitLine', 'Group', 'UID',
            'TimelimitRaw', 'CPUTimeRAW', 'TotalCPU', 'NNodes', 'ReqTRES', 'AllocTRES',
            'ReqMem', 'MaxRSS', 'ExitCode', 'NodeList', 'Priority']
# The step-row usage block the archived reducer discarded; feeds the I/O panel.
KEEP_NEW = ['AveDiskRead', 'AveDiskWrite', 'MaxDiskRead', 'MaxDiskWrite', 'MaxDiskReadNode',
            'MaxDiskWriteNode', 'TRESUsageInTot', 'TRESUsageOutTot', 'ConsumedEnergyRaw',
            'AveRSS', 'MaxVMSize', 'SystemCPU', 'UserCPU', 'NTasks', 'Reason',
            'Constraints', 'Comment']

# Derived columns, and the export columns each one needs.  A derived column whose
# source is absent is not written at all -- same honesty rule as KEEP_OPT.
DERIVED_SRC = [
    ('req_gpu', ('ReqTRES',)),
    ('alloc_gpu', ('AllocTRES',)),
    ('gpu_model', ('AllocTRES',)),
    ('gpuutil', ('TRESUsageInTot',)),
    ('gpumem', ('TRESUsageInTot',)),
    ('total_cpu_s', ('TotalCPU',)),
    ('system_cpu_s', ('SystemCPU',)),
    ('user_cpu_s', ('UserCPU',)),
    ('is_array', ()),
    ('array_job_id', ()),
    ('n_steps', ()),
    ('n_generations', ()),
    ('n_tasks', ('NTasks',)),
    ('max_disk_read_mb', ('MaxDiskRead',)),
    ('max_disk_write_mb', ('MaxDiskWrite',)),
    ('ave_disk_read_mb', ('AveDiskRead',)),
    ('ave_disk_write_mb', ('AveDiskWrite',)),
    ('max_rss_bytes', ('MaxRSS',)),
    ('ave_rss_bytes', ('AveRSS',)),
    ('max_vmsize_bytes', ('MaxVMSize',)),
    ('in_disk_mb', ('TRESUsageInTot',)),
    ('out_disk_mb', ('TRESUsageOutTot',)),
    ('energy_j', ('ConsumedEnergyRaw',)),
    ('energy_kwh', ('ConsumedEnergyRaw',)),
]

# Folding rules.  (column, node column or None)
MAX_COLS = [('MaxDiskRead', 'MaxDiskReadNode'), ('MaxDiskWrite', 'MaxDiskWriteNode'),
            ('MaxVMSize', None), ('MaxRSS', None)]
# `Ave*` prefers the .batch step and otherwise takes the max.  It is NEVER averaged
# across steps: sacct's Ave* is already a per-step mean over NTasks tasks, so a mean of
# means across steps of differing NTasks is not the job's mean of anything.
AVE_COLS = ['AveDiskRead', 'AveDiskWrite', 'AveRSS']
# hh:mm:ss durations: summed over step rows, with the job row as fallback (below).
CPU_COLS = ['TotalCPU', 'SystemCPU', 'UserCPU']

NOTICE_RETRY = ("The repeated-submission panel needs the SubmitLine column, which is not "
                "in the configured sacct export's --format. Add SubmitLine to SACCT_FORMAT "
                "in collect/slurm_query.bash and re-pull.")

STATES = ["COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "CANCELLED", "PREEMPTED",
          "NODE_FAIL", "REQUEUED"]

# Path/command markers an agent leaves in the accounting record.  Ported verbatim from
# rc_real.AGENT_MARKERS: the accounting tier carries no actor field, so this is its only
# agent signal (the authoritative per-submission actor is the login-node process tier).
AGENT_MARKERS = [
    ("claude-code", r"\.claude\b|claude-tmp|claude-code|/claude/"),
    ("codex", r"\.codex\b|/codex/|run_.*codex|codex-cli"),
    ("cursor", r"\.cursor\b|/cursor/"),
    ("aider", r"\.aider\b|aider-chat"),
]
AGENT_RE = {name: re.compile(rx, re.I) for name, rx in AGENT_MARKERS}


class SacctFormatError(ValueError):
    """An export missing a KEEP_CORE column: unusable, not merely degraded."""


# ---------------------------------------------------------------- scalar parsing
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMGTPE]?)i?[Bb]?\s*$")
_MULT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4,
         "P": 1024 ** 5, "E": 1024 ** 6}


def size_bytes(s):
    """sacct size (``1.5M``, ``2.3G``, ``1024K``, ``69039304K``) -> bytes, or None.

    One parser for every size field in the export.  A **bare** number is bytes: that
    is how sacct writes the ``*DiskRead``/``*DiskWrite`` and ``TRESUsage*`` ``fs/disk``
    counters, and ``MaxVMSize=0``.  Suffixes are binary (sacct's own convention),
    hence 1024 rather than 1000.
    """
    if s is None:
        return None
    m = _SIZE_RE.match(s)
    if not m:
        return None
    return float(m.group(1)) * _MULT[m.group(2)]


def hms(s):
    """sacct duration (``05:09:38``, ``1-09:08:48``, ``13:29.270``) -> seconds, or ''."""
    if not s:
        return ''
    d = 0
    if '-' in s:
        dd, s = s.split('-', 1)
        try:
            d = int(dd)
        except ValueError:
            return ''
    parts = s.split(':')
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return ''
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, sec = parts[-3], parts[-2], parts[-1]
    return round(d * 86400 + h * 3600 + m * 60 + sec, 2)


def tres_get(s, key):
    """One scalar out of a TRES list (``gres/gpuutil=43``, ``fs/disk=40397700700``)."""
    m = re.search(r'(?:^|,)' + re.escape(key) + r'=([^,]+)', s or '')
    return m.group(1) if m else ''


def _num(s, cast=float, default=None):
    try:
        return cast(s)
    except (TypeError, ValueError):
        return default


def _pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else 0.0


def _quants(values, qs=(0.1, 0.25, 0.5, 0.75, 0.9, 0.99)):
    """Quantiles + mean + n.  Ported from rc_real._quants (nearest-rank, no interp)."""
    if not values:
        return {}
    vs = sorted(values)
    out = {}
    for q in qs:
        i = min(len(vs) - 1, max(0, int(round(q * (len(vs) - 1)))))
        out["p%g" % (q * 100)] = round(vs[i], 4)
    out["mean"] = round(sum(vs) / len(vs), 4)
    out["n"] = len(vs)
    return out


def _r(v, n=3):
    """Round for output, but keep an integral value integral.

    ``ElapsedRaw``/``CPUTimeRAW``/``NTasks`` are whole-number columns in the export; a
    merged sum of them must not start rendering as ``97407.0``, or a reader that parses
    them with ``int()`` breaks on data it used to handle.
    """
    if not isinstance(v, float):
        return v
    if v.is_integer() and abs(v) < 2 ** 53:
        return int(v)
    return round(v, n)


# ---------------------------------------------------------------- source discovery
def find_sacct_inputs(dirpath, pattern="SlurmData_*.csv", max_days=0):
    """Day-files under ``dirpath``, **oldest first**.

    The filenames embed the date range (``SlurmData_2026-06-01_2026-06-02-orig.csv``)
    in ISO order, so lexical sort == chronological sort.  ``max_days > 0`` keeps the
    last N, i.e. the most recent N day-files.
    """
    if not dirpath or not os.path.isdir(dirpath):
        return []
    files = sorted(p for p in glob.glob(os.path.join(dirpath, pattern))
                   if os.path.isfile(p))
    if max_days and max_days > 0:
        files = files[-max_days:]
    return files


def find_sacct_csv(dirpath=None, pattern="SlurmData_*.csv"):
    """The **newest** day-file, or None.

    ``rc_real.find_sacct_csv`` (line 95-98) did ``for p in sorted(glob(...)): return p``,
    which returns the *oldest* file -- the opposite of what its caller wanted and of
    what its own docstring implied.
    """
    files = find_sacct_inputs(dirpath or REPO, pattern)
    return files[-1] if files else None


def is_raw_export(path):
    """True if ``path`` is a raw ``____``-delimited export rather than a reduced CSV."""
    try:
        with open(path, errors="replace") as fh:
            return DELIM in fh.readline()
    except OSError:
        return False


def read_header(path):
    with open(path, errors="replace") as fh:
        line = fh.readline().rstrip("\n")
    return line.split(DELIM) if DELIM in line else next(csv.reader([line]), [])


# ---------------------------------------------------------------- column plan
class Plan:
    """Which columns this *set* of inputs can support, and which it cannot.

    Headers genuinely differ between pulls -- the stale ``archive/.cache/`` reduction
    came from a 78-column export while the raw file beside it has 68 -- so the plan is
    the union over the inputs' headers, and a column present in only some of them is
    reported as ``partial`` rather than passed off as fully populated.
    """

    def __init__(self, headers):
        self.n_inputs = len(headers)
        seen = collections.Counter()
        for h in headers:
            seen.update(set(h))
        # KEEP_CORE is required of EVERY input, not of the union: one day-file missing
        # WorkDir would otherwise be masked by its siblings and silently reduce to a
        # file whose agent attribution is blank for that day only.  Checked here, before
        # a single row is written, so a bad input cannot leave a half-written cache.
        for h in headers:
            missing_core = [c for c in KEEP_CORE if c not in set(h)]
            if missing_core:
                raise SacctFormatError(
                    "sacct export lacks required column(s) %s -- add them to SACCT_FORMAT "
                    "in collect/slurm_query.bash and re-pull" % ", ".join(missing_core))
        self.keep = [c for c in KEEP_CORE + KEEP_OPT + KEEP_NEW if seen[c]]
        self.present = set(self.keep)
        self.missing = [c for c in KEEP_OPT + KEEP_NEW if not seen[c]]
        self.present_optional = [c for c in KEEP_OPT + KEEP_NEW if seen[c]]
        self.partial = [c for c in self.keep if 0 < seen[c] < self.n_inputs]
        self.derived = [c for c, src in DERIVED_SRC
                        if all(s in self.present for s in src)]
        self.columns = self.keep + self.derived

    def has(self, *cols):
        return all(c in self.present for c in cols)

    def report(self):
        return {"columns": list(self.columns), "keep": list(self.keep),
                "missing": list(self.missing),
                "present_optional": list(self.present_optional),
                "partial": list(self.partial)}


# ---------------------------------------------------------------- the fold
def _new_rec(p, idx, plan):
    rec = {c: (p[idx[c]] if c in idx else '') for c in plan.keep}
    rec['_acc'] = {'steps': 0, 'max': {}, 'ave': {}, 'batch': {}, 'cpu': {},
                   'in_disk': 0.0, 'out_disk': 0.0, 'energy': 0.0, 'energy_seen': False,
                   'gpuutil': None, 'gpumem': None, 'ntasks': None, 'disk_seen': False}
    return rec


def _fold_step(rec, p, idx, step_id):
    """Harvest one step row into its job row.  Called once per ``JobID.step`` line."""
    a = rec['_acc']
    a['steps'] += 1
    is_batch = step_id.endswith('.batch')

    for col, node_col in MAX_COLS:
        if col not in idx:
            continue
        v = p[idx[col]]
        if not v:
            continue
        b = size_bytes(v)
        if b is None:
            continue
        cur = a['max'].get(col)
        if cur is None or b > cur[0]:
            node = p[idx[node_col]] if (node_col and node_col in idx) else ''
            a['max'][col] = (b, v, node)

    for col in AVE_COLS:
        if col not in idx:
            continue
        v = p[idx[col]]
        if not v:
            continue
        b = size_bytes(v)
        if b is None:
            continue
        if is_batch:
            a['batch'][col] = (b, v)
        cur = a['ave'].get(col)
        if cur is None or b > cur[0]:
            a['ave'][col] = (b, v)

    for col in CPU_COLS:
        if col not in idx:
            continue
        secs = hms(p[idx[col]])
        if secs != '':
            a['cpu'][col] = a['cpu'].get(col, 0.0) + secs

    if 'ConsumedEnergyRaw' in idx:
        j = _num(p[idx['ConsumedEnergyRaw']], float, None)
        if j is not None:
            a['energy'] += j
            a['energy_seen'] = True

    if 'NTasks' in idx:
        nt = _num(p[idx['NTasks']], int, None)
        if nt is not None:
            a['ntasks'] = nt if a['ntasks'] is None else max(a['ntasks'], nt)

    if 'TRESUsageInTot' in idx:
        tu = p[idx['TRESUsageInTot']]
        if tu:
            d = size_bytes(tres_get(tu, 'fs/disk'))
            if d is not None:
                a['in_disk'] += d
                a['disk_seen'] = True
            u = _num(tres_get(tu, 'gres/gpuutil'), float, None)
            if u is not None:
                a['gpuutil'] = u if a['gpuutil'] is None else max(a['gpuutil'], u)
            g = size_bytes(tres_get(tu, 'gres/gpumem'))
            if g is not None:
                a['gpumem'] = g if a['gpumem'] is None else max(a['gpumem'], g)
            e = _num(tres_get(tu, 'energy'), float, None)
            if e is not None and not a['energy_seen']:
                a['energy'] += e
    if 'TRESUsageOutTot' in idx:
        tu = p[idx['TRESUsageOutTot']]
        if tu:
            d = size_bytes(tres_get(tu, 'fs/disk'))
            if d is not None:
                a['out_disk'] += d
                a['disk_seen'] = True


def _finish(rec, plan):
    """Close a job: apply the folded step values, derive scalars, drop bulky raws."""
    a = rec.pop('_acc')
    p = plan.present

    for col, _node in MAX_COLS:
        if col in p and col in a['max']:
            rec[col] = a['max'][col][1]
    for col, node_col in MAX_COLS:
        if node_col and node_col in p and col in a['max']:
            rec[node_col] = a['max'][col][2]
    for col in AVE_COLS:
        if col not in p:
            continue
        pick = a['batch'].get(col) or a['ave'].get(col)
        if pick:
            rec[col] = pick[1]
    if 'ConsumedEnergyRaw' in p and a['energy_seen']:
        rec['ConsumedEnergyRaw'] = a['energy']
    if 'NTasks' in p and a['ntasks'] is not None:
        rec['NTasks'] = a['ntasks']

    base = rec['JobID']
    if 'ReqTRES' in p:
        rec['req_gpu'] = tres_get(rec['ReqTRES'], 'gres/gpu')
    if 'AllocTRES' in p:
        alloc = rec['AllocTRES'] or ''
        rec['alloc_gpu'] = tres_get(alloc, 'gres/gpu')
        m = re.search(r'gres/gpu:([A-Za-z0-9_.\-]+)=', alloc)
        rec['gpu_model'] = m.group(1) if m else ''
    if 'TRESUsageInTot' in p:
        rec['gpuutil'] = '' if a['gpuutil'] is None else _r(a['gpuutil'])
        rec['gpumem'] = '' if a['gpumem'] is None else _r(a['gpumem'])
        rec['in_disk_mb'] = _r(a['in_disk'] / 1048576.0) if a['disk_seen'] else ''
        rec['TRESUsageInTot'] = ''            # bulky raw column: scalars extracted above
    if 'TRESUsageOutTot' in p:
        rec['out_disk_mb'] = _r(a['out_disk'] / 1048576.0) if a['disk_seen'] else ''
        rec['TRESUsageOutTot'] = ''
    # Durations: the ``hh:mm:ss`` column keeps sacct's own spelling and the seconds go to
    # a derived ``*_s`` column, so nothing downstream has to guess which it is holding.
    # The step sum wins when the steps reported one; a job with no step rows (cancelled
    # before launch, a step-less allocation) keeps slurmdbd's own job-row aggregate
    # rather than being zeroed.  Measured on the 06-01 export the two agree exactly.
    for col, dst in (('TotalCPU', 'total_cpu_s'), ('SystemCPU', 'system_cpu_s'),
                     ('UserCPU', 'user_cpu_s')):
        if col in p:
            rec[dst] = _r(a['cpu'][col], 2) if col in a['cpu'] else hms(rec[col])
    rec['is_array'] = '1' if '_' in base else '0'
    rec['array_job_id'] = base.split('_')[0] if '_' in base else base
    rec['n_steps'] = a['steps']
    rec['n_generations'] = 1        # raised by merge_generations() in reduce_many pass 2
    if 'NTasks' in p:
        rec['n_tasks'] = rec['NTasks'] or ''
    for dst, src in (('max_disk_read_mb', 'MaxDiskRead'), ('max_disk_write_mb', 'MaxDiskWrite'),
                     ('ave_disk_read_mb', 'AveDiskRead'), ('ave_disk_write_mb', 'AveDiskWrite')):
        if src in p:
            b = size_bytes(rec[src])
            rec[dst] = _r(b / 1048576.0) if b is not None else ''
    for dst, src in (('max_rss_bytes', 'MaxRSS'), ('ave_rss_bytes', 'AveRSS'),
                     ('max_vmsize_bytes', 'MaxVMSize')):
        if src in p:
            b = size_bytes(rec[src])
            rec[dst] = _r(b, 0) if b is not None else ''
    if 'ConsumedEnergyRaw' in p:
        j = _num(rec['ConsumedEnergyRaw'], float, None)
        rec['energy_j'] = _r(j, 1) if j is not None else ''
        rec['energy_kwh'] = _r(j / 3.6e6, 6) if j is not None else ''
    return rec


def _fold_file(path, plan, stats):
    """Stream one raw export, yielding one folded dict per job row.

    ``idx`` is built from **this file's own** header, so a directory whose day-files
    came from different pulls (different ``--format``) still reduces correctly; a
    column this file lacks is emitted empty and was already flagged in ``plan.partial``.
    """
    with open(path, errors="replace") as fh:
        hdr = fh.readline().rstrip("\n").split(DELIM)
        idx = {c: i for i, c in enumerate(hdr)}
        missing_core = [c for c in KEEP_CORE if c not in idx]
        if missing_core:
            raise SacctFormatError("%s lacks required column(s) %s"
                                   % (path, ", ".join(missing_core)))
        nf = len(hdr)
        pending = {}
        for line in fh:
            stats['lines'] += 1
            p = line.rstrip("\n").split(DELIM)
            if len(p) != nf:
                stats['ragged'] += 1
                continue
            jid = p[idx['JobID']]
            base = jid.split('.')[0]
            if '.' in jid:
                rec = pending.get(base)
                if rec is None:
                    stats['orphan_steps'] += 1
                    continue
                _fold_step(rec, p, idx, jid)
                continue
            # a new job row closes the previous one: steps always follow their job row
            for r in pending.values():
                yield _finish(r, plan)
            pending.clear()
            pending[base] = _new_rec(p, idx, plan)
        for r in pending.values():                     # last job in the file
            yield _finish(r, plan)


def iter_jobs(paths, plan=None, stats=None):
    """One folded job dict per job, over a list of raw exports, in file order.

    No de-duplication here -- that is ``reduce_many``'s job, because it needs to *pick*
    between two copies of the same job and a generator cannot retract a row it yielded.
    """
    paths = [paths] if isinstance(paths, str) else list(paths)
    plan = plan or Plan([read_header(p) for p in paths])
    stats = stats if stats is not None else collections.Counter()
    for path in paths:
        stats['files'] += 1
        for rec in _fold_file(path, plan, stats):
            stats['rows_read'] += 1
            yield rec


# ---------------------------------------------------------------- generation merge
# Two different things make one JobID appear more than once, and they need OPPOSITE
# treatment, so they are told apart by ``Start``:
#
#   * **Distinct generations.**  ``sacct -D`` emits one record per *run*: a preempted
#     and requeued job has several, each with its own ``Start``.  The cluster really
#     spent all of it, so the consumed quantities are SUMMED.  On the 06-01 export all
#     10 270 duplicates are of this kind (e.g. JobID 16362620, PREEMPTED three times
#     with 66 913 s + 829 s + 29 665 s elapsed).
#   * **The same generation observed twice.**  A run spanning midnight lands in both
#     day-files with the SAME ``Start``; summing there would double-count, so one
#     observation is kept -- the longer-``ElapsedRaw`` one, because the earlier day's
#     query caught the run mid-flight.
#
# If ``Start`` cannot separate them (every generation has an empty ``Start``: a job
# cancelled before dispatch, or a column that never filled) the merge falls back to
# de-duplication, counted in ``merged_no_start``.  Under-counting is the safer failure.

# Consumed quantities: summed across generations.
SUM_NUM = ['ElapsedRaw', 'CPUTimeRAW', 'ConsumedEnergyRaw', 'total_cpu_s', 'system_cpu_s',
           'user_cpu_s', 'in_disk_mb', 'out_disk_mb', 'energy_j', 'energy_kwh', 'n_steps']
# Same, but the export spells them hh:mm:ss; the summed seconds live in the `*_s`
# column and the text column is re-rendered from it.
SUM_HMS = [('TotalCPU', 'total_cpu_s'), ('SystemCPU', 'system_cpu_s'),
           ('UserCPU', 'user_cpu_s')]
# High-water marks: max across generations, carrying the derived twin (and the node
# that held a MaxDisk* peak).  Ave* is here rather than in SUM_NUM because sacct's Ave*
# is a per-step mean over NTasks tasks -- neither a sum nor a mean of means across
# generations is that job's average of anything, so the peak generation is reported.
MAX_SIZE = [('MaxRSS', 'max_rss_bytes', None), ('MaxVMSize', 'max_vmsize_bytes', None),
            ('AveRSS', 'ave_rss_bytes', None),
            ('MaxDiskRead', 'max_disk_read_mb', 'MaxDiskReadNode'),
            ('MaxDiskWrite', 'max_disk_write_mb', 'MaxDiskWriteNode'),
            ('AveDiskRead', 'ave_disk_read_mb', None),
            ('AveDiskWrite', 'ave_disk_write_mb', None)]
MAX_NUM = ['NTasks', 'n_tasks', 'gpuutil', 'gpumem']
EARLIEST = ['Submit', 'Start', 'Eligible']     # first submission / first dispatch
LATEST = ['End']                               # last generation's finish
# Everything else -- State, ExitCode, NodeList, Reason, Partition, the request columns --
# comes from the LAST generation, which is the one whose outcome the job ended on.


def fmt_hms(seconds):
    """Seconds -> sacct's ``[D-]HH:MM:SS``, for a re-rendered merged duration."""
    if seconds is None or seconds == '':
        return ''
    s = int(round(float(seconds)))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return ("%d-%02d:%02d:%02d" % (d, h, m, s)) if d else ("%02d:%02d:%02d" % (h, m, s))


def _elapsed_of(row):
    return _num(row.get('ElapsedRaw'), int, 0) or 0


def _pick_observation(rows, stats):
    """One row out of several observations of the SAME generation (same ``Start``).

    The longer ``ElapsedRaw`` wins, then the one with a non-empty ``End``: the copy
    written by an earlier day's query saw the run mid-flight.
    """
    best = rows[0]
    for r in rows[1:]:
        stats['dedup_same_start'] += 1
        stats['dropped_elapsed_s'] += min(_elapsed_of(r), _elapsed_of(best))
        if (_elapsed_of(r), bool(r.get('End'))) > (_elapsed_of(best), bool(best.get('End'))):
            best = r
    return best


def merge_generations(rows, cols, stats):
    """Fold every record of one JobID into a single row.  Returns that row.

    Counting grain stays one row per JobID -- a job preempted three times is one job,
    so the job / submission / user / work_dir counts must not triple -- while the
    resource grain sums, because the machine really did spend all of it.
    """
    groups = collections.OrderedDict()
    for r in rows:
        groups.setdefault(r.get('Start') or '', []).append(r)
    # Records with no Start cannot be told apart, so that subset collapses to one and
    # is counted here, whatever else this JobID carries: under-counting beats inventing
    # runs that may never have happened.
    if len(groups.get('', ())) > 1:
        stats['merged_no_start'] += 1
    gens = [_pick_observation(g, stats) for g in groups.values()]
    if len(gens) == 1:
        out = dict(gens[0])
        out['n_generations'] = 1
        return out

    gens.sort(key=lambda r: (r.get('Start') or ''))
    out = dict(gens[-1])                       # identity / terminal fields: last run
    stats['merged_jobs'] += 1
    stats['merged_generations'] += len(gens)

    for c in SUM_NUM:
        if c in cols:
            vals = [_num(r.get(c), float, None) for r in gens]
            vals = [v for v in vals if v is not None]
            out[c] = _r(sum(vals), 3) if vals else ''
    for text, secs in SUM_HMS:
        if text in cols:
            out[text] = fmt_hms(out.get(secs)) if out.get(secs) not in ('', None) else ''
    for raw, derived, node in MAX_SIZE:
        if raw not in cols:
            continue
        best = None
        for r in gens:
            b = size_bytes(r.get(raw))
            if b is not None and (best is None or b > best[0]):
                best = (b, r)
        if best is not None:
            out[raw] = best[1].get(raw, '')
            if derived in cols:
                out[derived] = best[1].get(derived, '')
            if node and node in cols:
                out[node] = best[1].get(node, '')
    for c in MAX_NUM:
        if c in cols:
            vals = [_num(r.get(c), float, None) for r in gens]
            vals = [v for v in vals if v is not None]
            out[c] = _r(max(vals), 3) if vals else ''
    for c in EARLIEST:
        if c in cols:
            vals = [r.get(c) for r in gens if r.get(c)]
            out[c] = min(vals) if vals else ''
    for c in LATEST:
        if c in cols:
            vals = [r.get(c) for r in gens if r.get(c)]
            out[c] = max(vals) if vals else ''
    out['n_generations'] = len(gens)
    return out


def reduce_many(paths, out, keep=None):
    """Stream raw day-files into one per-job CSV at ``out``.  Returns a report dict.

    ``keep`` restricts the kept export columns (derived columns follow); it is for
    tests and for a caller that wants a slim CSV, not for normal use.
    """
    paths = [paths] if isinstance(paths, str) else list(paths)
    if not paths:
        raise SacctFormatError("no sacct input files")
    plan = Plan([read_header(p) for p in paths])
    if keep:
        wanted = set(keep)
        plan.keep = [c for c in plan.keep if c in wanted or c in KEEP_CORE]
        plan.present = set(plan.keep)
        plan.derived = [c for c, src in DERIVED_SRC if all(s in plan.present for s in src)]
        plan.columns = plan.keep + plan.derived

    cols = plan.columns
    colset = set(cols)
    stats = collections.Counter()
    part = out + ".part"
    first_file = {}          # JobID -> index of the file it was first seen in
    extra = collections.Counter()      # JobID -> occurrences beyond the first
    try:
        # Pass 1: fold the multi-GB inputs, writing one row per *generation* and noting
        # which JobIDs occur more than once.  Generations of one job can sit far apart
        # in the file, and a generator cannot retract a row it already yielded, so the
        # merge happens in pass 2 rather than here.
        with open(part, 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for rec in iter_jobs(paths, plan, stats):
                jid = rec['JobID']
                fno = stats['files']
                if jid in first_file:
                    extra[jid] += 1
                    stats['dup_job_ids'] += 1
                    stats['dup_cross_file' if first_file[jid] != fno
                          else 'dup_same_file'] += 1
                else:
                    first_file[jid] = fno
                w.writerow([rec.get(c, '') for c in cols])
    except BaseException:
        # never leave a half-written .part where a later run could mistake it for output
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    n_jobs = len(first_file)
    del first_file

    if extra:
        # Pass 2 reads only the *reduced* CSV (~500 B/row, not the multi-GB input).
        # Single-generation jobs stream straight through, keeping their position; the
        # ~2% that need merging are buffered (10 270 rows on the 06-01 export) and
        # written at the end.
        held = collections.defaultdict(list)
        try:
            with open(part, newline='') as src, open(out, 'w', newline='') as dst:
                rd = csv.DictReader(src)
                w = csv.writer(dst)
                w.writerow(cols)
                for row in rd:
                    jid = row['JobID']
                    if jid in extra:
                        held[jid].append(row)
                    else:
                        w.writerow([row.get(c, '') for c in cols])
                for jid, rows in held.items():
                    merged = merge_generations(rows, colset, stats)
                    w.writerow([merged.get(c, '') for c in cols])
        except BaseException:
            for p in (part, out):
                try:
                    os.remove(p)
                except OSError:
                    pass
            raise
        os.remove(part)
    else:
        os.replace(part, out)

    rep = {"files": len(paths), "lines": stats['lines'], "rows": n_jobs,
           "dup_job_ids": stats['dup_job_ids'], "rows_read": stats['rows_read'],
           "dup_cross_file": stats['dup_cross_file'],
           "dup_same_file": stats['dup_same_file'],
           "merged_jobs": stats['merged_jobs'],
           "merged_generations": stats['merged_generations'],
           "dedup_same_start": stats['dedup_same_start'],
           "merged_no_start": stats['merged_no_start'],
           "dropped_elapsed_s": stats['dropped_elapsed_s'],
           "ragged_lines": stats['ragged'], "orphan_steps": stats['orphan_steps']}
    rep.update(plan.report())
    print("sacct reduce: %d file(s), %d lines -> %d job rows "
          "(%d extra record(s): %d generations summed into %d jobs, %d same-Start "
          "observation(s) de-duplicated)"
          % (rep['files'], rep['lines'], rep['rows'], rep['dup_job_ids'],
             rep['merged_generations'], rep['merged_jobs'], rep['dedup_same_start']),
          file=sys.stderr)
    if rep['missing']:
        print("sacct reduce: columns absent from the export: %s"
              % ", ".join(rep['missing']), file=sys.stderr)
    return rep


def reduce(src, out):
    """Back-compat wrapper for ``archive/reduce_sacct.reduce``: returns the row count."""
    return reduce_many([src], out)["rows"]


# ---------------------------------------------------------------- fingerprint cache
def fingerprint(paths):
    """Per-input identity: path, size, mtime **and the hash of its header line**.

    The header hash is the part the retired mtime check lacked.  A re-pull with a
    different ``--format`` can land with any mtime; only the header tells you the
    columns changed, and serving a reduction of the old columns is silent corruption.
    """
    inputs = []
    for p in paths:
        st = os.stat(p)
        hdr = read_header(p)
        inputs.append({"path": os.path.abspath(p), "size": st.st_size,
                       "mtime": round(st.st_mtime, 6), "n_cols": len(hdr),
                       "header_sha1": hashlib.sha1(
                           DELIM.join(hdr).encode("utf-8", "replace")).hexdigest()})
    return inputs


def _key(inputs, reducer_version, keep):
    blob = json.dumps({"v": CACHE_VERSION, "rv": reducer_version, "keep": keep,
                       "inputs": inputs}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _ident(inputs):
    return [[i["path"], i["size"], i["mtime"]] for i in inputs]


def cache_stale(meta, inputs, reducer_version):
    """Why the cache must be rebuilt, or None if it may be reused."""
    if not meta:
        return "no sidecar"
    if meta.get("version") != CACHE_VERSION:
        return "cache version %r != %r" % (meta.get("version"), CACHE_VERSION)
    if meta.get("reducer_version") != reducer_version:
        return "reducer version %r != %r" % (meta.get("reducer_version"), reducer_version)
    old = meta.get("inputs") or []
    if _ident(old) != _ident(inputs):
        return "input set changed (path/size/mtime)"
    if [i.get("header_sha1") for i in old] != [i["header_sha1"] for i in inputs]:
        return "export header changed"
    return None


def _prev_meta(cache_dir, inputs):
    """A sidecar in ``cache_dir`` for the same input paths, for explaining a rebuild.

    Prefers one whose recorded ``(path, size, mtime)`` still matches the inputs, so the
    reason names the header change rather than the size/mtime difference it would
    otherwise notice first; falls back to the most recently built.
    """
    paths = [i["path"] for i in inputs]
    ident = _ident(inputs)
    newest = None
    for name in sorted(glob.glob(os.path.join(cache_dir, "jobs_*.meta.json"))):
        try:
            with open(name) as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        old = m.get("inputs") or []
        if [i.get("path") for i in old] != paths:
            continue
        if _ident(old) == ident:
            return m
        if newest is None or (m.get("built_at") or "") >= (newest.get("built_at") or ""):
            newest = m
    return newest


def prune_cache(cache_dir, keep_key, keep=5):
    """Keep the ``keep`` newest ``jobs_<key>.csv`` reductions; drop older ones.

    Only files this module wrote (``jobs_`` + 12 hex + ``.csv``/``.meta.json``) are
    considered, and the live key is never removed.  A fingerprint key changes whenever
    an input's mtime does, so without this the cache dir grows one reduction per pull.
    """
    pat = re.compile(r"^jobs_[0-9a-f]{12}\.csv$")
    try:
        names = [n for n in os.listdir(cache_dir) if pat.match(n)]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(cache_dir, n)), reverse=True)
    dropped = []
    for n in names[max(1, keep):]:
        if n == "jobs_%s.csv" % keep_key:
            continue
        for path in (os.path.join(cache_dir, n),
                     os.path.join(cache_dir, n[:-4] + ".meta.json")):
            try:
                os.remove(path)
                dropped.append(path)
            except OSError:
                pass
    return dropped


def ensure_reduced(paths, cache_dir=None, reducer_version=REDUCER_VERSION, keep=None,
                   force=False, prune=5):
    """Reduce ``paths`` into the cache if needed; return where the per-job CSV is.

    ``{"csv", "meta", "rebuilt", "reason", "key"}``.  Safe to call on every request:
    a hit costs one ``stat`` plus one header read per input.
    """
    paths = [paths] if isinstance(paths, str) else list(paths)
    cache_dir = cache_dir or os.path.join(HERE, ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    inputs = fingerprint(paths)
    key = _key(inputs, reducer_version, keep)
    csv_path = os.path.join(cache_dir, "jobs_%s.csv" % key)
    meta_path = csv_path[:-4] + ".meta.json"

    meta = None
    if os.path.exists(csv_path) and os.path.exists(meta_path):
        try:
            with open(meta_path) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = None
    else:
        meta = None
    reason = "forced" if force else cache_stale(meta, inputs, reducer_version)
    if reason == "no sidecar":
        # The fingerprint is part of the cache *path*, so a changed input lands on a
        # fresh name and its sidecar is simply absent.  Say WHICH change caused that by
        # diffing against the newest sidecar for the same input paths.
        prev = _prev_meta(cache_dir, inputs)
        if prev:
            reason = "new fingerprint: %s" % cache_stale(prev, inputs, reducer_version)
    if reason is None:
        return {"csv": csv_path, "meta": meta, "rebuilt": False, "reason": None, "key": key}

    rep = reduce_many(paths, csv_path, keep=keep)
    meta = {"version": CACHE_VERSION, "reducer_version": reducer_version,
            "inputs": inputs, "keep": rep["keep"], "keep_filter": keep,
            "columns": rep["columns"],
            "rows": rep["rows"], "dup_job_ids": rep["dup_job_ids"],
            "missing": rep["missing"], "present_optional": rep["present_optional"],
            "partial": rep["partial"], "lines": rep["lines"], "files": rep["files"],
            "dup_cross_file": rep["dup_cross_file"],
            "dup_same_file": rep["dup_same_file"],
            "merged_jobs": rep["merged_jobs"],
            "merged_generations": rep["merged_generations"],
            "dedup_same_start": rep["dedup_same_start"],
            "merged_no_start": rep["merged_no_start"],
            "dropped_elapsed_s": rep["dropped_elapsed_s"],
            "built_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    tmp = meta_path + ".part"
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)
    os.replace(tmp, meta_path)
    if prune:
        prune_cache(cache_dir, key, prune)
    return {"csv": csv_path, "meta": meta, "rebuilt": True, "reason": reason, "key": key}


# ---------------------------------------------------------------- identity side-table
def load_domains(path=None):
    """user -> (domain, lab) from the committed job-type mapping table."""
    path = path or os.path.join(REPO, "dataclean", "mapping", "map_user.csv")
    if not path or not os.path.exists(path):
        return {}
    out = {}
    with open(path, errors="replace") as fh:
        for row in csv.DictReader(fh):
            u = row.get("user")
            if u:
                out[u] = (row.get("top_domain") or "unknown", row.get("lab") or "")
    return out


def agent_kind(work_dir, submit_line=None):
    """Which agent product left a marker in the accounting record, if any.

    With ``SubmitLine`` absent from the export this sees ``WorkDir`` only, so the
    command-only alternatives (``run_.*codex``, ``codex-cli``, ``aider-chat``) can
    never fire: **recall** drops, precision is unchanged.  ``rollup`` reports that in
    ``_attribution`` rather than letting the reader assume full coverage.
    """
    blob = (work_dir or "") + " " + (submit_line or "")
    for name in AGENT_RE:
        if AGENT_RE[name].search(blob):
            return name
    return None


# ---------------------------------------------------------------- rollup
def _rows(paths_or_csv, stats):
    """Sniff raw-vs-reduced (never guess from the name) and stream rows either way.

    Ported from ``rc_real.rollup_sacct``'s inner ``rows()``, with one fix: the raw
    path now runs the **full fold**, where the archived version yielded job rows only
    -- so every usage field (``MaxRSS``, ``TotalCPU``, and now the whole disk/TRES
    block) read empty for every job whenever a raw export was rolled up directly.
    """
    paths = [paths_or_csv] if isinstance(paths_or_csv, str) else list(paths_or_csv)
    if not paths:
        raise SacctFormatError("no sacct input files")
    raw = [p for p in paths if is_raw_export(p)]
    if raw and len(raw) != len(paths):
        raise SacctFormatError("mixed raw exports and reduced CSVs: %s" % paths)
    if raw:
        # A single streaming pass cannot merge generations: they sit far apart in the
        # file and a row already counted cannot be retracted.  ``rollup`` therefore
        # reduces a raw input through the cache by default, and this path is reached
        # only when the caller passed ``allow_raw_stream=True`` and accepted that its
        # totals are NOT generation-summed.
        plan = Plan([read_header(p) for p in paths])
        seen = set()
        for rec in iter_jobs(paths, plan):
            if rec['JobID'] in seen:
                stats['dup_job_ids'] += 1
                continue
            seen.add(rec['JobID'])
            yield plan, rec
        return
    for p in paths:
        with open(p, errors="replace") as fh:
            rd = csv.DictReader(fh)
            plan = _PlanOfColumns(rd.fieldnames or [])
            for r in rd:
                yield plan, r


class _PlanOfColumns:
    """The columns a reduced CSV actually has, for the same ``has()`` question."""

    def __init__(self, fieldnames):
        self.present = set(fieldnames)
        self.columns = list(fieldnames)
        self.missing = [c for c in KEEP_OPT + KEEP_NEW if c not in self.present]

    def has(self, *cols):
        return all(c in self.present for c in cols)


def _io_of(d, plan):
    """Per-job I/O scalars in MB/GB, from the derived columns or the raw strings.

    These live on **step** rows, exactly like ``MaxRSS``: a job with no step rows never
    gets one, so every total below ships with its own coverage percentage.
    """
    def mb(derived, raw):
        """MB, preferring the reducer's derived column and falling back to the raw."""
        v = _num(d.get(derived), float, None)
        if v is not None:
            return v
        b = size_bytes(d.get(raw))
        return b / 1048576.0 if b is not None else None

    def mb_of_bytes(derived, raw):
        """Same, for a derived column that carries **bytes** rather than MB."""
        b = _num(d.get(derived), float, None)
        if b is None:
            b = size_bytes(d.get(raw))
        return b / 1048576.0 if b is not None else None
    out = {
        "max_disk_read_mb": mb("max_disk_read_mb", "MaxDiskRead"),
        "max_disk_write_mb": mb("max_disk_write_mb", "MaxDiskWrite"),
        "ave_disk_read_mb": mb("ave_disk_read_mb", "AveDiskRead"),
        "ave_disk_write_mb": mb("ave_disk_write_mb", "AveDiskWrite"),
        "max_vmsize_mb": mb_of_bytes("max_vmsize_bytes", "MaxVMSize"),
        "ave_rss_mb": mb_of_bytes("ave_rss_bytes", "AveRSS"),
        "tres_in_mb": _num(d.get("in_disk_mb"), float, None),
        "tres_out_mb": _num(d.get("out_disk_mb"), float, None),
        "energy_kwh": _num(d.get("energy_kwh"), float, None),
        "ntasks": _num(d.get("n_tasks") or d.get("NTasks"), float, None),
    }
    if out["energy_kwh"] is None:
        j = _num(d.get("energy_j") or d.get("ConsumedEnergyRaw"), float, None)
        if j is not None:
            out["energy_kwh"] = j / 3.6e6
    return out


def rollup(paths_or_csv, cfg=None, report=None, domains=None, cache_dir=None,
           allow_raw_stream=False):
    """Stream the per-job CSV (or the raw export) into the dashboard's sacct panels.

    Grains follow the repo rule: every agent-vs-other statement is reported at all
    four units -- job (array-expanded), submission (array-collapsed), user and
    work_dir -- and the per-user / per-work_dir views are distributions, not just
    counts, because a single mega-user otherwise *is* the agent headline.

    Handed a **raw** export, this reduces it through the fingerprint cache first and
    rolls up the result, so the numbers are identical to any other reader of that
    cache.  Streaming a raw export directly cannot merge requeue generations -- a
    single pass cannot retract a row it already counted -- so it would quietly return
    different totals; ``allow_raw_stream=True`` opts into that, and the result then
    carries ``_dedup`` saying the totals are not generation-summed.
    """
    if not allow_raw_stream:
        paths = [paths_or_csv] if isinstance(paths_or_csv, str) else list(paths_or_csv)
        if paths and all(is_raw_export(p) for p in paths):
            if cache_dir is None and cfg is not None:
                getter = getattr(cfg, "get", None)
                cache_dir = getter("cache.dir") if getter else None
            got = ensure_reduced(paths, cache_dir=cache_dir)
            res = rollup(got["csv"], cfg=cfg, report=report, domains=domains)
            res["_reduced_via"] = {"csv": got["csv"], "rebuilt": got["rebuilt"],
                                   "reason": got["reason"],
                                   "rows": (got["meta"] or {}).get("rows"),
                                   "merged_jobs": (got["meta"] or {}).get("merged_jobs"),
                                   "merged_generations":
                                       (got["meta"] or {}).get("merged_generations")}
            return res
    if domains is None:
        dpath = None
        if cfg is not None:
            getter = getattr(cfg, "get", None)
            dpath = getter("feeds.domains.path") if getter else None
        domains = load_domains(dpath)

    n_rows = 0
    win = [None, None]
    plan = None
    cls_jobs = collections.Counter()
    cls_state = collections.defaultdict(collections.Counter)
    cls_subs = collections.defaultdict(set)
    cls_users = collections.defaultdict(set)
    cls_wd = collections.defaultdict(set)
    cls_user_jobs = collections.defaultdict(collections.Counter)
    cls_wd_jobs = collections.defaultdict(collections.Counter)
    cls_coreh = collections.Counter()
    cls_gpuh = collections.Counter()
    cls_gpujobs = collections.Counter()
    cls_arrays = collections.Counter()
    cls_requeued = collections.Counter()
    cls_single = collections.Counter()
    cls_walluse = collections.defaultdict(list)
    cls_cpueff = collections.defaultdict(list)
    cls_memeff = collections.defaultdict(list)
    cls_queue = collections.defaultdict(list)
    cls_elapsed = collections.defaultdict(list)
    cls_hour = collections.defaultdict(collections.Counter)
    cls_tl = collections.defaultdict(list)
    by_product = collections.Counter()
    prod_state = collections.defaultdict(collections.Counter)
    user_jobs = collections.Counter()
    user_cls = {}
    partitions = collections.Counter()
    domain_jobs = collections.defaultdict(collections.Counter)
    subline = collections.Counter()
    subline_cls = {}
    gpu_class = collections.defaultdict(collections.Counter)
    timeline = collections.defaultdict(collections.Counter)
    # I/O accumulators: totals, their denominators, and the per-job samples for quants
    io_n = collections.Counter()
    io_disk_n = collections.Counter()
    io_tres_n = collections.Counter()
    io_energy_n = collections.Counter()
    io_energy_nz = collections.Counter()
    io_tot = collections.defaultdict(collections.Counter)
    io_q = collections.defaultdict(lambda: collections.defaultdict(list))
    stats = collections.Counter()

    for plan, d in _rows(paths_or_csv, stats):
        n_rows += 1
        prod = agent_kind(d.get("WorkDir"), d.get("SubmitLine"))
        c = "agent" if prod else "other"
        state = (d.get("State") or "?").split()[0]
        user = d.get("User") or "?"
        sub = d.get("Submit") or ""
        if sub:
            if win[0] is None or sub < win[0]:
                win[0] = sub
            if win[1] is None or sub > win[1]:
                win[1] = sub
            cls_hour[c][int(sub[11:13]) if len(sub) >= 13 else 0] += 1
            timeline[sub[:13]][c] += 1

        cls_jobs[c] += 1
        cls_state[c][state] += 1
        cls_subs[c].add(d.get("array_job_id") or d.get("JobID"))
        cls_users[c].add(user)
        cls_user_jobs[c][user] += 1
        if d.get("WorkDir"):
            cls_wd[c].add(d["WorkDir"])
            cls_wd_jobs[c][d["WorkDir"]] += 1
        if d.get("is_array") == "1":
            cls_arrays[c] += 1
        ngen = _num(d.get("n_generations"), int, 1) or 1
        stats['generations'] += ngen
        if ngen > 1:
            stats['requeued_jobs'] += 1
            cls_requeued[c] += 1
        user_jobs[user] += 1
        user_cls[user] = c if user_cls.get(user, c) == c else "mixed"
        if d.get("Partition"):
            partitions[d["Partition"]] += 1
        dom = domains.get(user, ("unknown", ""))[0]
        domain_jobs[c][dom] += 1
        if prod:
            by_product[prod] += 1
            prod_state[prod][state] += 1
            sl = (d.get("SubmitLine") or "")[:300]
            if sl:
                subline[(user, sl)] += 1
                subline_cls[(user, sl)] = prod

        ncpus = _num(d.get("NCPUS"), int, 0) or 0
        elapsed = _num(d.get("ElapsedRaw"), int, 0) or 0
        tl = _num(d.get("TimelimitRaw"), int, 0) or 0          # minutes
        agpu = _num(d.get("alloc_gpu"), int, 0) or 0
        if ncpus == 1:
            cls_single[c] += 1
        cls_coreh[c] += ncpus * elapsed / 3600.0
        if agpu:
            cls_gpujobs[c] += 1
            cls_gpuh[c] += agpu * elapsed / 3600.0
        if elapsed:
            cls_elapsed[c].append(elapsed)
        if tl:
            cls_tl[c].append(tl * 60)
            if elapsed:
                cls_walluse[c].append(min(1.5, elapsed / (tl * 60.0)))
        tcpu = _num(d.get("total_cpu_s"), float, None)
        if tcpu is None:
            tcpu = _num(hms(d.get("TotalCPU")), float, None)
        if tcpu and elapsed > 60 and ncpus:
            cls_cpueff[c].append(min(1.5, tcpu / (elapsed * ncpus)))
        rss = size_bytes(d.get("MaxRSS")) or _num(d.get("max_rss_bytes"), float, None)
        reqmem = size_bytes(d.get("ReqMem"))
        if rss and reqmem:
            cls_memeff[c].append(min(1.5, rss / reqmem))
        st, sb = d.get("Start") or "", d.get("Submit") or ""
        if st[:2] == "20" and sb[:2] == "20":
            try:
                q = (dt.datetime.strptime(st, "%Y-%m-%dT%H:%M:%S")
                     - dt.datetime.strptime(sb, "%Y-%m-%dT%H:%M:%S")).total_seconds()
                if 0 <= q < 30 * 86400:
                    cls_queue[c].append(q)
            except ValueError:
                pass
        if agpu:
            util = _num(d.get("gpuutil"), float, None)
            gpu_class[c]["gpu_used" if (util or 0) > 0 else
                         ("gpu_idle" if util is not None else "gpu_util_unknown")] += 1
        elif (_num(d.get("req_gpu"), int, 0) or 0):
            gpu_class[c]["gpu_requested_unmet"] += 1

        io = _io_of(d, plan)
        # "has I/O" deliberately ignores ConsumedEnergyRaw: on this cluster that column
        # is populated for 99.9% of jobs and reads 0 for all of them (energy accounting
        # is off), so counting it would advertise ~100% I/O coverage for a panel whose
        # disk numbers really cover ~88%.
        if any(io[k] is not None for k in ("max_disk_read_mb", "max_disk_write_mb",
                                           "ave_disk_read_mb", "ave_disk_write_mb",
                                           "tres_in_mb", "tres_out_mb")):
            io_n[c] += 1
        if io["max_disk_read_mb"] is not None or io["max_disk_write_mb"] is not None:
            io_disk_n[c] += 1
        if io["tres_in_mb"] is not None or io["tres_out_mb"] is not None:
            io_tres_n[c] += 1
        if io["energy_kwh"] is not None:
            io_energy_n[c] += 1
            if io["energy_kwh"] > 0:
                io_energy_nz[c] += 1
        for k, v in io.items():
            if v is None:
                continue
            io_tot[c][k] += v
            io_q[c][k].append(v)

    plan = plan or _PlanOfColumns([])
    have_submitline = plan.has("SubmitLine")

    def per(c):
        return {
            "jobs": cls_jobs[c],
            "submissions": len(cls_subs[c]),
            "users": len(cls_users[c]),
            "work_dirs": len(cls_wd[c]),
            "core_hours": round(cls_coreh[c], 1),
            "gpu_hours": round(cls_gpuh[c], 1),
            "gpu_jobs": cls_gpujobs[c],
            "array_pct": _pct(cls_arrays[c], cls_jobs[c]),
            "requeued_jobs": cls_requeued[c],
            "requeued_pct": _pct(cls_requeued[c], cls_jobs[c]),
            "single_core_pct": _pct(cls_single[c], cls_jobs[c]),
            "array_collapse": round(cls_jobs[c] / max(1, len(cls_subs[c])), 2),
            "state_pct": {s: _pct(cls_state[c][s], cls_jobs[c]) for s in STATES},
            "state_n": {s: cls_state[c][s] for s in STATES},
            "walltime_used_frac": _quants(cls_walluse[c]),
            "cpu_eff": _quants(cls_cpueff[c]),
            "mem_eff": _quants(cls_memeff[c]),
            "queue_wait_s": _quants(cls_queue[c]),
            "elapsed_s": _quants(cls_elapsed[c]),
            "timelimit_s": _quants(cls_tl[c]),
            # per-capita grains: each user / each work_dir is one unit, so a single
            # mega-array campaign cannot carry the class
            "jobs_per_user": _quants([float(v) for v in cls_user_jobs[c].values()]),
            "jobs_per_work_dir": _quants([float(v) for v in cls_wd_jobs[c].values()]),
            "hour_of_day": [cls_hour[c].get(h, 0) for h in range(24)],
            "gpu_class": dict(gpu_class[c]),
            "top_domains": domain_jobs[c].most_common(8),
        }

    def io_per(c):
        t = io_tot[c]
        return {
            "jobs": cls_jobs[c],
            "jobs_with_io": io_n[c],
            "coverage_pct": _pct(io_n[c], cls_jobs[c]),
            "disk_jobs": io_disk_n[c],
            "disk_cov_pct": _pct(io_disk_n[c], cls_jobs[c]),
            "disk_read_gb": round(t["max_disk_read_mb"] / 1024.0, 2),
            "disk_write_gb": round(t["max_disk_write_mb"] / 1024.0, 2),
            "tres_jobs": io_tres_n[c],
            "tres_cov_pct": _pct(io_tres_n[c], cls_jobs[c]),
            "tres_in_gb": round(t["tres_in_mb"] / 1024.0, 2),
            "tres_out_gb": round(t["tres_out_mb"] / 1024.0, 2),
            "energy_jobs": io_energy_n[c],
            "energy_cov_pct": _pct(io_energy_n[c], cls_jobs[c]),
            # a populated ConsumedEnergyRaw that reads 0 means the counter exists but
            # the site is not accounting energy -- distinguish it from "no counter"
            "energy_nonzero_jobs": io_energy_nz[c],
            "energy_nonzero_pct": _pct(io_energy_nz[c], cls_jobs[c]),
            "energy_kwh": round(t["energy_kwh"], 3),
            "quants": {k: _quants(v) for k, v in sorted(io_q[c].items())},
        }

    repeats = [{"user": u, "n": n, "product": subline_cls[(u, sl)],
                "submit_line": sl[:180]}
               for (u, sl), n in subline.most_common(12) if n > 1]
    counts = sorted(user_jobs.values())
    tot = sum(counts) or 1
    cum = 0.0
    for i, v in enumerate(counts, 1):
        cum += i * v
    gini = round((2 * cum) / (len(counts) * tot) - (len(counts) + 1) / len(counts), 3) \
        if counts else 0.0

    result = {
        "window": {"submit_min": win[0], "submit_max": win[1], "job_rows": n_rows,
                   # one row per JobID; `generations` counts the sacct -D records those
                   # rows were merged from, so the two differ exactly by the requeues
                   "generations": stats['generations'],
                   "requeued_jobs": stats['requeued_jobs'],
                   "requeued_pct": _pct(stats['requeued_jobs'], n_rows)},
        "classes": {c: per(c) for c in ("agent", "other")},
        "io_jobs": {c: io_per(c) for c in ("agent", "other")},
        "by_product": dict(by_product),
        "product_state_pct": {p: {s: _pct(prod_state[p][s], by_product[p]) for s in STATES}
                              for p in by_product},
        "retry_repeats": repeats,
        "concentration": {
            "gini_user_jobs": gini,
            "n_users": len(user_jobs),
            "top10_share_pct": _pct(sum(n for _, n in user_jobs.most_common(10)), tot),
            "rank_size": [n for _, n in user_jobs.most_common(60)],
            "top_users": [{"user": u, "jobs": n, "class": user_cls.get(u, "other")}
                          for u, n in user_jobs.most_common(12)],
        },
        "partitions": partitions.most_common(14),
        "timeline": sorted(({"t": k, "agent": v.get("agent", 0), "other": v.get("other", 0)}
                            for k, v in timeline.items()), key=lambda r: r["t"]),
        "_columns_missing": list(getattr(plan, "missing", [])),
    }
    if stats['dup_job_ids']:
        msg = ("allow_raw_stream=True: %d extra record(s) for a JobID were DROPPED, not "
               "merged, so every consumed total here (core_hours, elapsed, cpu, disk) "
               "under-counts requeued/preempted jobs. Drop allow_raw_stream, or roll up "
               "ensure_reduced()['csv'], to get generation-summed numbers."
               % stats['dup_job_ids'])
        result["_dedup"] = {"dropped": stats['dup_job_ids'],
                            "rule": "first occurrence kept; generations NOT summed",
                            "note": msg}
        result.setdefault("_degraded", []).append("generation_merge")
        print("sacct rollup: " + msg, file=sys.stderr)
    result["io_jobs"]["_basis"] = (
        "disk_read_gb / disk_write_gb sum each job's per-job MAX across its steps "
        "(sacct exposes no job-level total); tres_* sum fs/disk from TRESUsage*Tot. "
        "All of it lives on step rows, like MaxRSS -- read every total next to its "
        "coverage_pct, never alone.")
    result["_attribution"] = {
        "basis": "WorkDir + SubmitLine" if have_submitline else "WorkDir only",
        "submit_line_available": bool(have_submitline),
        "recall": "full" if have_submitline else "reduced",
    }
    if not have_submitline:
        # Do NOT substitute a JobName proxy: JobName is absent from this export too, and
        # a different signal under the same panel title would be dishonest.
        result.setdefault("_degraded", []).append("SubmitLine")
        result["retry_repeats"] = []
        result["_notice_retry"] = NOTICE_RETRY
    if report is not None:
        result["_feed"] = report.get("name") if hasattr(report, "get") else None
        files = (report.get("files") or []) if hasattr(report, "get") else []
        result["_files"] = [os.path.basename(f["path"]) for f in files]
        result["_file"] = (os.path.basename(files[-1]["path"]) if files else None)
    return result


# ---------------------------------------------------------------- CLI
def _main(argv):
    if len(argv) < 3 or argv[1] in ("-h", "--help"):
        print(__doc__.strip().splitlines()[0])
        print("usage: python -m rc_dashboard.sacct reduce <out.csv> <raw export>...")
        print("       python -m rc_dashboard.sacct rollup <per-job csv|raw export>...")
        print("       python -m rc_dashboard.sacct cache  <cache dir> <raw export>...")
        return 2
    verb, rest = argv[1], argv[2:]
    if verb == "reduce":
        print(json.dumps(reduce_many(rest[1:], rest[0]), indent=1))
    elif verb == "rollup":
        print(json.dumps(rollup(rest), indent=1, default=str)[:20000])
    elif verb == "cache":
        got = ensure_reduced(rest[1:], cache_dir=rest[0])
        print(json.dumps({k: v for k, v in got.items() if k != "meta"}, indent=1))
        print("rebuilt=%s reason=%s" % (got["rebuilt"], got["reason"]))
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
