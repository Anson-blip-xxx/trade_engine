"""Read only systemd-delivered master material from a protected memory mount.

Provision LoadCredentialEncrypted outside this module. No plaintext-file/env
fallback, generation, persistence, or subprocess carrying secret bytes exists here.
"""

import os
import re
import stat
import struct
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

    @staticmethod
    def _protected(details, *, directory=False, acl=None):
        mode = stat.S_IMODE(details.st_mode)
        private_modes = {0o500, 0o700} if directory else {0o400}
        if details.st_uid in {0, os.geteuid()} and mode in private_modes:
            return True
        # ACL mask appears as group mode bits, but must grant only the service UID.
        if (
            details.st_uid != 0
            or mode != (0o550 if directory else 0o440)
            or acl is None
        ):
            return False
        permission = 5 if directory else 4
        expected = {
            (1, permission, 0xFFFFFFFF),
            (2, permission, os.geteuid()),
            (4, 0, 0xFFFFFFFF),
            (16, permission, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        }
        try:
            return (
                len(acl) == 44
                and struct.unpack("<I", acl[:4])[0] == 2
                and set(struct.iter_unpack("<HHI", acl[4:])) == expected
            )
        except (struct.error, TypeError):
            return False

    def _protected_fd(self, fd, *, directory=False):
        details = os.fstat(fd)
        acl = (
            os.getxattr(fd, "system.posix_acl_access")
            if stat.S_IMODE(details.st_mode) == (0o550 if directory else 0o440)
            else None
        )
        return self._protected(details, directory=directory, acl=acl)

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
            if not self._protected_fd(directory, directory=True):
                raise ValueError()
            fd = os.open("v2-master-" + key_id, flags | os.O_NONBLOCK, dir_fd=directory)
            descriptors.append(fd)
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or not self._protected_fd(fd)
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
