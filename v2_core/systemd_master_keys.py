"""Read only systemd-delivered master material from a protected memory mount.

Provision LoadCredentialEncrypted outside this module. No plaintext-file/env
fallback, generation, persistence, or subprocess carrying secret bytes exists here.
"""

import os
import re
import stat
from pathlib import Path

from v2_core.credential_vault import VaultError


def secure_mount(fd):
    """Descriptor mount identity avoids pathname/mount-prefix ambiguity."""
    info = Path(f"/proc/self/fdinfo/{fd}").read_text()
    mount_id = next(
        line.split()[1] for line in info.splitlines() if line.startswith("mnt_id:")
    )
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, backing = left.split(), right.split()
        if fields[0] == mount_id:
            return "ro" in fields[5].split(",") and (
                backing[0] == "ramfs"
                or (backing[0] == "tmpfs" and "noswap" in backing[2].split(","))
            )
    return False


class SystemdMasterKeys:
    ROOT = Path("/run/credentials")

    def __init__(self, directory):
        self.directory = Path(directory)
        if self.directory.parent != self.ROOT or not re.fullmatch(
            r"[A-Za-z0-9_.@:-]+", self.directory.name
        ):
            raise VaultError("SYSTEMD_CREDENTIAL_DIRECTORY_REQUIRED")

    def __call__(self, key_id):
        if not isinstance(key_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", key_id
        ):
            raise VaultError("MASTER_KEY_UNAVAILABLE")
        descriptors = []
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            root = os.open(self.ROOT, flags | os.O_DIRECTORY)
            descriptors.append(root)
            directory = os.open(
                self.directory.name, flags | os.O_DIRECTORY, dir_fd=root
            )
            descriptors.append(directory)
            details = os.fstat(directory)
            if (
                details.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise ValueError()
            fd = os.open("v2-master-" + key_id, flags | os.O_NONBLOCK, dir_fd=directory)
            descriptors.append(fd)
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or stat.S_IMODE(details.st_mode) != 0o400
                or details.st_uid not in {0, os.geteuid()}
                or details.st_size != 32
                or not secure_mount(fd)
            ):
                raise ValueError()
            material = os.read(fd, 33)
            if len(material) != 32:
                raise ValueError()
            return material
        except Exception:  # noqa: BLE001 - never expose credential path or material
            raise VaultError("MASTER_KEY_UNAVAILABLE") from None
        finally:
            for fd in reversed(descriptors):
                os.close(fd)
