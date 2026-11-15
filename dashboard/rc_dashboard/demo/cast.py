"""The fictional site: its users, their labs and projects, and how each works.

Every name here is made up for the demo. Homes are `/home/<user>` and scratch
`/scratch/<lab>/<user>`, so the anonymizer's default patterns find them when a
demo is run with RC_DASH_ANONYMIZE=1. Lab names end in `_lab` for the same reason.

A person's `kinds` are the sessions they start, weighted: what a session of each
kind does is in `agents.py` and `sessions.py`. `hours` is when their working day
starts and how long it runs, in local hours; `days` how likely they are to work
on a weekday and on a weekend -- set high on weekends, where agents are busy too,
so a demo opened on a Sunday still has a day to show.
"""

# Compute nodes a login node's commands name (ssh, srun --nodelist): matched by
# the default risk profile's compute_hosts (`holy|bos|node|gpu|c\d{3,}`).
COMPUTE = ("gpu-a07", "gpu-a12", "gpu-b03", "node1203", "node1418", "c0412")
PARTITIONS = {"gpu": "gpu", "cpu": "shared", "test": "test", "big": "bigmem"}

PROJECTS = {
    "vit-pruning": {
        "lang": "python", "dir": "projects/vit-pruning",
        "files": ["src/prune.py", "src/models/vit.py", "src/data/loader.py", "src/train.py",
                  "configs/base.yaml", "scripts/eval.py"],
        "tests": ["tests/test_prune.py", "tests/test_loader.py"],
        "jobs": ["jobs/train.sbatch", "jobs/sweep.sbatch"],
        "symbols": ["magnitude_prune", "DataLoader", "attention_mask", "head_importance"],
        "topics": [
            ("Fix the shuffle seed in the data loader", "src/data/loader.py", "tests/test_loader.py"),
            ("Add structured head pruning", "src/prune.py", "tests/test_prune.py"),
            ("Log pruning ratios to the eval report", "scripts/eval.py", "tests/test_prune.py"),
            ("Resume training from the last checkpoint", "src/train.py", "tests/test_loader.py"),
            ('Add a warmup schedule to the pruning ratio', 'src/prune.py', 'tests/test_prune.py'),
            ('Stop the eval script from reloading the checkpoint twice', 'scripts/eval.py', 'tests/test_prune.py'),
            ('Mask padded tokens in the attention', 'src/models/vit.py', 'tests/test_prune.py'),
            ('Move the dataset paths into the config', 'configs/base.yaml', 'tests/test_loader.py'),
        ],
    },
    "galaxy-sim": {
        "lang": "cpp", "dir": "src/galaxy-sim",
        "files": ["src/nbody.cpp", "src/tree.cpp", "include/tree.hpp", "src/io/hdf5_out.cpp",
                  "CMakeLists.txt"],
        "tests": ["tests/test_tree.cpp", "tests/test_energy.cpp"],
        "jobs": ["slurm/run_small.sh", "slurm/run_scaling.sh"],
        "symbols": ["BarnesHut", "theta", "leapfrog", "write_snapshot"],
        "topics": [
            ("Fix the energy drift in the leapfrog step", "src/nbody.cpp", "tests/test_energy.cpp"),
            ("Speed up the octree build", "src/tree.cpp", "tests/test_tree.cpp"),
            ("Write snapshots in parallel HDF5", "src/io/hdf5_out.cpp", "tests/test_tree.cpp"),
            ('Clamp the opening angle for tiny cells', 'src/tree.cpp', 'tests/test_tree.cpp'),
            ('Add an energy check to the CI run', 'tests/test_energy.cpp', 'tests/test_energy.cpp'),
            ('Read initial conditions from HDF5', 'src/io/hdf5_out.cpp', 'tests/test_tree.cpp'),
        ],
    },
    "regrid": {
        "lang": "python", "dir": "work/regrid",
        "files": ["regrid.py", "era5_fetch.py", "plots/anomaly.py", "env.yml"],
        "tests": ["tests/test_regrid.py"],
        "jobs": ["jobs/regrid_year.sh"],
        "symbols": ["conservative_weights", "era5", "anomaly"],
        "topics": [
            ("Regrid the ERA5 temperatures to 1 degree", "regrid.py", "tests/test_regrid.py"),
            ("Plot the 2023 anomaly maps", "plots/anomaly.py", "tests/test_regrid.py"),
            ("Cache the conservative weights on scratch", "regrid.py", "tests/test_regrid.py"),
            ('Fetch ERA5 by month instead of by year', 'era5_fetch.py', 'tests/test_regrid.py'),
            ('Pin the environment for the regrid job', 'env.yml', 'tests/test_regrid.py'),
        ],
    },
    "lattice-qcd": {
        "lang": "python", "dir": "lattice-qcd",
        "files": ["hmc/integrator.py", "hmc/action.py", "analysis/correlators.py",
                  "analysis/fit_mass.py"],
        "tests": ["tests/test_integrator.py", "tests/test_action.py"],
        "jobs": ["runs/hmc_l16.sbatch", "runs/hmc_l24.sbatch"],
        "symbols": ["omelyan", "wilson_action", "effective_mass"],
        "topics": [
            ("Switch the HMC integrator to Omelyan", "hmc/integrator.py", "tests/test_integrator.py"),
            ("Fit the pion mass from the correlators", "analysis/fit_mass.py", "tests/test_action.py"),
            ("Tune the step size for L=24", "hmc/action.py", "tests/test_integrator.py"),
            ('Bin the correlators before the fit', 'analysis/correlators.py', 'tests/test_action.py'),
            ('Fix the reversibility test of the integrator', 'hmc/integrator.py', 'tests/test_integrator.py'),
            ('Log acceptance rates per trajectory', 'hmc/action.py', 'tests/test_integrator.py'),
        ],
    },
    "compiler-passes": {
        "lang": "rust", "dir": "code/compiler-passes",
        "files": ["src/passes/dce.rs", "src/passes/inline.rs", "src/ir/builder.rs",
                  "Cargo.toml"],
        "tests": ["tests/dce.rs", "tests/inline.rs"],
        "jobs": [],
        "symbols": ["DeadCodeElim", "inline_threshold", "BlockBuilder"],
        "topics": [
            ("Handle phi nodes in dead-code elimination", "src/passes/dce.rs", "tests/dce.rs"),
            ("Add a cost model to the inliner", "src/passes/inline.rs", "tests/inline.rs"),
            ('Keep debug locations through inlining', 'src/passes/inline.rs', 'tests/inline.rs'),
            ('Build blocks in reverse post-order', 'src/ir/builder.rs', 'tests/dce.rs'),
            ('Remove unreachable blocks after DCE', 'src/passes/dce.rs', 'tests/dce.rs'),
        ],
    },
    "rnaseq-pipeline": {
        "lang": "python", "dir": "pipelines/rnaseq",
        "files": ["Snakefile", "rules/align.smk", "rules/count.smk", "scripts/qc_report.py",
                  "config/samples.tsv"],
        "tests": ["tests/test_qc.py"],
        "jobs": ["slurm/align_array.sh"],
        "symbols": ["star_index", "featureCounts", "samples.tsv"],
        "topics": [
            ("Add the STAR alignment rule", "rules/align.smk", "tests/test_qc.py"),
            ("Summarise QC across samples", "scripts/qc_report.py", "tests/test_qc.py"),
            ("Submit the alignment as a job array", "slurm/align_array.sh", "tests/test_qc.py"),
            ('Count reads per gene with featureCounts', 'rules/count.smk', 'tests/test_qc.py'),
            ('Validate the sample sheet before the run', 'config/samples.tsv', 'tests/test_qc.py'),
        ],
    },
}

# user, uid, lab, project, hours (start, length), days (weekday, weekend),
# kinds {kind: weight}, sessions per active day (lo, hi), host slot
PEOPLE = [
    {"user": "ada", "uid": 21001, "lab": "ml_lab", "project": "vit-pruning",
     "hours": (9, 9), "days": (0.95, 0.6), "kinds": {"claude_terminal": 1, "claude_nested": 2, "claude_babysit": 1},
     "sessions": (1, 2), "slot": 0, "approval": "supervised", "slurm": True},
    {"user": "alan", "uid": 21002, "lab": "astro_lab", "project": "galaxy-sim",
     "hours": (10, 11), "days": (0.9, 0.6), "kinds": {"codex_tui": 4, "human_shell": 1},
     "sessions": (1, 2), "slot": 1, "approval": "supervised", "slurm": True},
    {"user": "grace", "uid": 21003, "lab": "climate_lab", "project": "regrid",
     "hours": (14, 10), "days": (0.85, 0.7), "kinds": {"claude_terminal": 3},
     "sessions": (1, 2), "slot": 0, "approval": "bypassed", "slurm": False,
     "login_compute": True},
    {"user": "emmy", "uid": 21004, "lab": "physics_lab", "project": "lattice-qcd",
     "hours": (8, 9), "days": (0.9, 0.4), "kinds": {"claude_desktop": 3},
     "sessions": (1, 2), "slot": 1, "approval": "auto", "slurm": True},
    {"user": "edsger", "uid": 21005, "lab": "cs_lab", "project": "compiler-passes",
     "hours": (9, 9), "days": (0.9, 0.5), "kinds": {"vscode_claude": 3, "vscode": 1},
     "sessions": (1, 1), "slot": 0, "approval": "supervised", "slurm": False},
    {"user": "barbara", "uid": 21006, "lab": "bio_lab", "project": "rnaseq-pipeline",
     "hours": (9, 8), "days": (0.95, 0.4), "kinds": {"human_shell": 1},
     "sessions": (1, 2), "slot": 1, "slurm": True},
    # runs agents from cron around the clock
    {"user": "kat", "uid": 21007, "lab": "ml_lab", "project": "vit-pruning",
     "hours": (0, 24), "days": (1.0, 1.0),
     "kinds": {"codex_exec": 3, "claude_print": 2, "claude_nightly": 1},
     "sessions": (9, 15), "slot": 0, "approval": "bypassed", "slurm": False},
    {"user": "frances", "uid": 21008, "lab": "cs_lab", "project": "compiler-passes",
     "hours": (18, 5), "days": (0.6, 0.8), "kinds": {"claude_rc": 1},
     "sessions": (1, 1), "slot": 1, "approval": "supervised", "slurm": False},
    {"user": "john", "uid": 21009, "lab": "astro_lab", "project": "galaxy-sim",
     "hours": (10, 8), "days": (0.8, 0.3), "kinds": {"vscode": 1},
     "sessions": (1, 1), "slot": 0, "slurm": False},
    {"user": "dorothy", "uid": 21010, "lab": "bio_lab", "project": "rnaseq-pipeline",
     "hours": (9, 7), "days": (0.9, 0.4), "kinds": {"claude_terminal": 1},
     "sessions": (1, 2), "slot": 1, "approval": "supervised", "slurm": True,
     "keeps_log": "PROGRESS.md", "sandboxed": True},
    {"user": "leslie", "uid": 21011, "lab": "climate_lab", "project": "regrid",
     "hours": (11, 8), "days": (0.85, 0.5), "kinds": {"codex_app": 1},
     "sessions": (1, 1), "slot": 0, "approval": "unobservable", "slurm": False},
    {"user": "tim", "uid": 21012, "lab": "physics_lab", "project": "lattice-qcd",
     "hours": (12, 6), "days": (0.7, 0.4), "kinds": {"human_shell": 1},
     "sessions": (1, 1), "slot": 1, "slurm": True, "mover": True},
]


def home(p):
    return "/home/%s" % p["user"]


def scratch(p):
    return "/scratch/%s/%s" % (p["lab"], p["user"])


def project_dir(p):
    return "%s/%s" % (home(p), PROJECTS[p["project"]]["dir"])


def hosts_of(p, hosts):
    """The login node a person uses: their slot, wrapped over however many there are."""
    return hosts[p["slot"] % len(hosts)]


# The rest of the floor: people the tabs need for a crowd but no story -- most at
# a shell or in an editor, some with an agent. Drawn once from a fixed generator,
# so every seed has the same people; what they do each day is the seed's.
CROWD = ["liam", "maya", "noor", "omar", "priya", "quinn", "rosa", "sam", "theo", "uma",
         "victor", "wen", "xin", "yara", "zeke", "ines"]
# Never close their editor: a VS Code server each, connected day and night, whose
# git polling is what a login node records at 3 a.m.
PARKED = ("wen", "xin")


def _crowd():
    import random
    rng = random.Random("rc_dashboard.demo.cast.crowd")
    labs = sorted({p["lab"] for p in PEOPLE})
    projects = sorted(PROJECTS)
    out = []
    for i, user in enumerate(CROWD):
        agent = rng.random() < 0.6
        kinds = ({"claude_terminal": 3, "codex_tui": 1, "human_shell": 2} if agent
                 else {"human_shell": 3, "vscode": 1})
        if agent and rng.random() < 0.4:
            kinds["claude_babysit"] = 1
        parked = user in PARKED
        out.append({"user": user, "uid": 22001 + i, "lab": rng.choice(labs),
                    "project": rng.choice(projects),
                    "hours": (0, 24) if parked else (rng.choice((8, 9, 10, 11, 13)), rng.choice((5, 7, 9))),
                    "days": (1.0, 1.0) if parked else (0.85, 0.45),
                    "kinds": {"vscode_parked": 1} if parked else kinds,
                    "sessions": (1, 1) if parked else (1, 2), "slot": i,
                    "approval": rng.choice(("supervised", "supervised", "bypassed", "auto")),
                    "slurm": rng.random() < 0.6})
    return out


PEOPLE = PEOPLE + _crowd()
