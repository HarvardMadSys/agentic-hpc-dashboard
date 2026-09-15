"""Slurm resource-request flags out of a submit tool's own argv.

VENDORED: parse_cli_time is analyze/switch_submitline_time.py's, verbatim except
that it returns None rather than numpy.nan — the collector has no numpy and a
JSON null is the honest absent value. See VENDORED.md.

Scope note, deliberate: this reads the tool's ARGV and nothing else. It does NOT
open the submitted script to scan #SBATCH directives, because the script path
comes from work_dir, which is a path read from /proc, and resolving one of those
is what the collector forbids everywhere else — on a login node work_dir is
almost always NFS, and following it onto a wedged autofs mount blocks the
single-threaded collector during the exact incident it exists to observe.
The directives are recovered downstream instead, by joining submit.job_id to the
slurm_jobs census, which already carries partition / time_limit / array_* /
gres and is authoritative: it is what Slurm granted, not what the script asked
for.
"""
import re

_TIME = re.compile(r'(?:--time[ =]|(?<![\w-])-t[ =])([0-9:\-]+)')
_PART = re.compile(r'(?:--partition[ =]|(?<![\w-])-p[ =])([\w,.\-]+)')
_ARRAY = re.compile(r'(?:--array[ =]|(?<![\w-])-a[ =])([\w,.\-:%]+)')
_MEM = re.compile(r'--mem(?:-per-cpu|-per-gpu)?[ =]([\w.]+)')
_CPT = re.compile(r'(?:--cpus-per-task[ =]|(?<![\w-])-c[ =])(\d+)')
# gpu | gpu:N | gpu:type:N. The type must be letter-led, or an optional-type
# group swallows the ':N' of `gpu:2` and the count silently reads as 1.
_GRES = re.compile(r'--gres[ =]gpu(?::[A-Za-z][\w.]*)?(?::(\d+))?')
_GPUS = re.compile(r'--gpus(?:-per-node|-per-task)?[ =](?:[\w]+:)?(\d+)')


def parse_cli_time(tok):
    """Slurm --time forms: minutes | m:s | h:m:s | d-h | d-h:m | d-h:m:s -> seconds."""
    try:
        dash = "-" in tok
        days = 0
        if dash:
            d, tok = tok.split("-", 1)
            days = int(d)
        p = [int(x) for x in tok.split(":") if x != ""]
        if dash:
            h = p[0]; m = p[1] if len(p) > 1 else 0; s = p[2] if len(p) > 2 else 0
        elif len(p) == 1:
            h, m, s = 0, p[0], 0
        elif len(p) == 2:
            h, m, s = 0, p[0], p[1]
        else:
            h, m, s = p[0], p[1], p[2]
        return days * 86400 + h * 3600 + m * 60 + s
    except Exception:
        return None


def parse_submit_args(args):
    """Resource request from an sbatch/salloc/srun command line.

    Every field is None when the flag is absent — absent means "not requested on
    the command line", which is NOT the same as "not requested" (it may be in the
    script). req_src says which, so a consumer never mistakes one for the other."""
    out = {'partition': None, 'gpus': None, 'array': None, 'time_limit_s': None,
           'mem': None, 'cpus_per_task': None, 'req_src': None}
    if not args:
        return out
    m = _PART.search(args)
    if m:
        out['partition'] = m.group(1)
    m = _ARRAY.search(args)
    if m:
        out['array'] = m.group(1)
    m = _MEM.search(args)
    if m:
        out['mem'] = m.group(1)
    m = _CPT.search(args)
    if m:
        out['cpus_per_task'] = int(m.group(1))
    m = _TIME.search(args)
    if m:
        out['time_limit_s'] = parse_cli_time(m.group(1))
    m = _GRES.search(args)
    if m:
        # `--gres=gpu` with no count means 1.
        out['gpus'] = int(m.group(1)) if m.group(1) else 1
    else:
        m = _GPUS.search(args)
        if m:
            out['gpus'] = int(m.group(1))
    if any(v is not None for k, v in out.items() if k != 'req_src'):
        out['req_src'] = 'argv'
    return out
