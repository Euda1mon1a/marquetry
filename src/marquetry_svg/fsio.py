"""Output files are written without following symlinks, and replaced all at once.

Marquetry writes next to the input image by default, which may be a shared folder such as
Downloads. A symlink planted as that default output folder, or under one of its output names,
must not redirect a write somewhere else. So the output folder is opened once (O_NOFOLLOW unless
the user named the folder explicitly, and it must be a directory owned by the current user),
and every later check, create, rename and delete happens relative to that open folder.
Swapping the folder for a symlink afterwards changes nothing.

replace_all checks every output name first (each must be absent or a regular file, and none
may be one of the inputs), writes each new file under a temporary name, renames them into
place, and only then removes outputs of an earlier run that this run doesn't produce. A refusal
or failure therefore leaves the earlier outputs as they were.
"""
import errno
import os
import secrets
import stat
from pathlib import Path


def is_real_dir(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def identities(*paths):
    """{(device, inode)} of the files that exist among PATHS (inputs that must not be overwritten,
    whatever case or spelling an output name uses)."""
    ids = set()
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        ids.add((st.st_dev, st.st_ino))
    return ids


class OutDir:
    """An output folder, opened once. Use as a context manager; names are plain file names.

    explicit=True is for a folder the user named (--out, --out-prefix, an outdir argument):
    a symlink there is followed, once, at open. The default folder next to the image must not
    be a symlink. Files inside are never followed either way."""

    def __init__(self, path, *, explicit=False):
        self.path = Path(path).expanduser().absolute()
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            st = None
        if st is not None and stat.S_ISLNK(st.st_mode) and not explicit:
            raise ValueError(f"refusing to write into {self.path}: it is a symlink")
        if st is not None and not stat.S_ISLNK(st.st_mode) and not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"{self.path} exists and is not a folder")
        if st is None:
            self.path.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | (0 if explicit else os.O_NOFOLLOW)
        try:
            self.fd = os.open(self.path, flags)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError(f"refusing to write into {self.path}: it is not a real folder") from None
            raise
        st = os.fstat(self.fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            os.close(self.fd)
            raise ValueError(f"refusing to write into {self.path}: it is not a folder you own")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _check(name):
        if not name or name in (".", "..") or "/" in name or "\0" in name:
            raise ValueError(f"not a plain file name: {name!r}")

    def lstat(self, name):
        self._check(name)
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _replaceable(self, name, protect):
        st = self.lstat(name)
        if st is None:
            return
        if not stat.S_ISREG(st.st_mode):
            kind = "a symlink" if stat.S_ISLNK(st.st_mode) else "not a regular file"
            raise ValueError(f"refusing to replace {self.path / name}: it is {kind}")
        if (st.st_dev, st.st_ino) in protect:
            raise ValueError(f"{self.path / name} is one of the inputs; an output would overwrite "
                             "it. Rename it or choose another output folder")

    def replace_all(self, files, *, stale=(), protect=frozenset()):
        """Write FILES ({name: str or bytes}) and remove STALE names this run doesn't produce.
        Every name is checked before anything changes; PROTECT holds (device, inode) pairs of
        inputs."""
        names = list(files) + [n for n in stale if n not in files]
        for n in names:
            self._replaceable(n, protect)
        temps = {}
        try:
            for n, data in files.items():
                tmp = f".{n}.{secrets.token_hex(6)}.tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                             dir_fd=self.fd)
                temps[n] = tmp
                with os.fdopen(fd, "wb") as f:
                    f.write(data.encode("utf-8") if isinstance(data, str) else data)
            for n in list(temps):
                os.rename(temps[n], n, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                del temps[n]
        finally:
            for tmp in temps.values():
                try:
                    os.unlink(tmp, dir_fd=self.fd)
                except OSError:
                    pass
        for n in names[len(files):]:
            st = self.lstat(n)
            if st is not None and stat.S_ISREG(st.st_mode):
                os.unlink(n, dir_fd=self.fd)
