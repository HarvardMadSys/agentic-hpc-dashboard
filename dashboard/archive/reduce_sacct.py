#!/usr/bin/env python3
"""Reduce a raw sacct export to one row per job, for the dashboard builder.

The export (``____``-delimited, 78 columns) carries a **job** row plus one row per
**step**; the request lives on the job row and the usage (``MaxRSS``, ``TotalCPU``,
``TRESUsageInTot`` -> ``gres/gpuutil``) lives on the step rows.  This streams the file
once, folds each job's steps into its job row, and drops the bulky raw TRES column, so
a 1.6 GB export becomes a CSV the builder can re-read cheaply.

    python3 dashboard/reduce_sacct.py SlurmData_2026-06-01.csv jobs.csv

``build_dashboard_data.py`` calls :func:`reduce` itself when handed a raw export, and
caches the result under ``dashboard/.cache/``.
"""
import csv, sys, re, os

KEEP = ['JobID','User','Account','QOS','Partition','JobName','State','Submit','Eligible','Start','End',
        'ElapsedRaw','TimelimitRaw','CPUTimeRAW','TotalCPU','NCPUS','NNodes','ReqTRES','AllocTRES',
        'ReqMem','MaxRSS','WorkDir','SubmitLine','ExitCode','NodeList','Priority','TRESUsageInTot','Group','UID']

def tres_get(s, key):
    m = re.search(r'(?:^|,)' + re.escape(key) + r'=([^,]+)', s or '')
    return m.group(1) if m else ''

def hms(s):
    """sacct TotalCPU like 05:09:38 or 1-09:08:48 or 13:29.270 -> seconds"""
    if not s: return ''
    d = 0
    if '-' in s:
        dd, s = s.split('-', 1); d = int(dd)
    parts = s.split(':')
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return ''
    while len(parts) < 3: parts.insert(0, 0.0)
    h, m, sec = parts[-3], parts[-2], parts[-1]
    return round(d*86400 + h*3600 + m*60 + sec, 2)

def reduce(SRC, OUT):
    """Stream ``SRC`` (raw export) into ``OUT`` (one row per job). Returns row count."""
    f = open(SRC, errors='replace')
    hdr = f.readline().rstrip('\n').split('____')
    idx = {c: i for i, c in enumerate(hdr)}
    out = open(OUT, 'w', newline='')
    w = csv.writer(out)
    cols = KEEP + ['req_gpu','alloc_gpu','gpu_model','gpumem','gpuutil','total_cpu_s','is_array','array_job_id']
    w.writerow(cols)
    n = jr = 0
    # batch/step rows carry MaxRSS + TRESUsage; job rows carry the request. Merge per JobID.
    pending = {}
    for line in f:
        n += 1
        p = line.rstrip('\n').split('____')
        if len(p) != len(hdr): continue
        jid = p[idx['JobID']]
        base = jid.split('.')[0]
        if '.' in jid:
            # step row: harvest usage into the pending job row
            rec = pending.get(base)
            if rec is None: continue
            tu = p[idx['TRESUsageInTot']]
            if not rec['MaxRSS']: rec['MaxRSS'] = p[idx['MaxRSS']]
            if not rec['TotalCPU'] or rec['TotalCPU'] in ('00:00:00',): rec['TotalCPU'] = p[idx['TotalCPU']]
            if tu:
                if not rec['gpuutil']: rec['gpuutil'] = tres_get(tu, 'gres/gpuutil')
                if not rec['gpumem']: rec['gpumem'] = tres_get(tu, 'gres/gpumem')
            continue
        # flush previous
        if pending:
            for k, rec in pending.items():
                rec['total_cpu_s'] = hms(rec['TotalCPU'])
                w.writerow([rec.get(c, '') for c in cols])
            pending.clear()
        jr += 1
        rec = {c: p[idx[c]] if c in idx else '' for c in KEEP}
        alloc = rec['AllocTRES']; req = rec['ReqTRES']
        rec['req_gpu'] = tres_get(req, 'gres/gpu')
        rec['alloc_gpu'] = tres_get(alloc, 'gres/gpu')
        m = re.search(r'gres/gpu:([A-Za-z0-9_.\-]+)=', alloc or '')
        rec['gpu_model'] = m.group(1) if m else ''
        rec['gpuutil'] = tres_get(rec['TRESUsageInTot'], 'gres/gpuutil')
        rec['gpumem'] = tres_get(rec['TRESUsageInTot'], 'gres/gpumem')
        rec['is_array'] = '1' if '_' in base else '0'
        rec['array_job_id'] = base.split('_')[0] if '_' in base else base
        rec['TRESUsageInTot'] = ''  # drop the bulky raw column
        pending[base] = rec
    for k, rec in pending.items():
        rec['total_cpu_s'] = hms(rec['TotalCPU'])
        w.writerow([rec.get(c, '') for c in cols])
    out.close()
    print('reduce_sacct: %d lines -> %d job rows' % (n, jr), file=sys.stderr)
    return jr


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit('usage: reduce_sacct.py <raw sacct export> <out per-job csv>')
    reduce(sys.argv[1], sys.argv[2])
