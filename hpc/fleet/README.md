# fleet: the guard on the nodes

The collector writes each login node's day-files on that node's own disk. The dashboard's
host copies them every minute with [`deploy/fleet/pull.py`](../../deploy/fleet/pull.py), the
copy the fleet kit runs too: rsync over ssh, with a key of its own. On a node where you have
no root, that key's line in `~/.ssh/authorized_keys` names the one program here as its forced
command. [`hpc/deploy`](../deploy/README.md) installs it and says which line to add; this page
is for running or changing it outside the kit.

| file | runs on | what it does |
|---|---|---|
| [`rsync_guard.py`](rsync_guard.py) | each node, as the copy key's forced command | lets that key run read-only rsync of the collector's folder, and nothing else |

## `rsync_guard.py`

```
restrict,from="<the dashboard's host>",command="/usr/bin/python3 <SENDER_DIR>/current/rsync_guard.py /scratch/ebpfm" ssh-ed25519 AAAA... rc-fleet-copy
```

sshd runs it in place of whatever the key asked for. It lets one command through, rsync
sending files from under the folders named after it (`rsync --server --sender ... . <path>`),
checked and then run as it was asked, with no shell between. Anything else is refused with
one line on stderr, which pull.py records as the pass's error, and exit 1: a shell or any
other command, rsync receiving (which would write on the node), an option that deletes,
writes, runs something or follows links out of the folders, and a path that does not lead
into them. It takes what pull.py sends and the read-only options a stock rsync client may
add, as rsync's own `rrsync -ro` does, so `rsync -av -e "ssh -i <key>"
<user>@<node>:/scratch/ebpfm/ here/` works by hand with the same key. The module docstring is
the reference: every option, every refusal and why.

`--rsync PATH` names the rsync to run (by default the first of `/usr/bin/rsync`,
`/usr/local/bin/rsync` and `/bin/rsync`). Exit 2 when it is installed wrong: no folder, one
that is not a full path, or no rsync there. It needs Python 3.6 or later and its standard
library only, so a node's `/usr/bin/python3` runs it with nothing installed. It is not
executable in git: the key line runs it with `python3`.

A key line from the kit's earlier stream runs `<SENDER_DIR>/current/fleet_stream.py send
--root <SOURCE_ROOT>`. `install-sender.sh` installs the guard under that name too, and the
guard takes those words, so such a line keeps working with nothing to change on the nodes.

## Tests

```bash
python3 -m unittest discover -s hpc/fleet/tests -q
```

They check the guard's choices word by word, run it as sshd does, and copy through it with
pull.py and real rsync (an ssh stand-in runs the guard as the forced command), clients that
try to write, delete or read outside the folder included.
