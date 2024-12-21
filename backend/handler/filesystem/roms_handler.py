import binascii
import bz2
import hashlib
import os
import re
import shutil
import tarfile
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final, Literal, TypedDict

import magic
import py7zr
import zipfile_deflate64  # trunk-ignore(ruff/F401): Patches zipfile to support deflate64 compression
from config import LIBRARY_BASE_PATH
from config.config_manager import config_manager as cm
from exceptions.fs_exceptions import RomAlreadyExistsException, RomsNotFoundException
from models.rom import Rom, RomFile
from py7zr.exceptions import (
    Bad7zFile,
    DecompressionError,
    PasswordRequired,
    UnsupportedCompressionMethodError,
)
from utils.archive_7zip import CallbackIOFactory
from utils.filesystem import iter_directories, iter_files
from utils.hashing import crc32_to_hex

from .base_handler import (
    LANGUAGES_BY_SHORTCODE,
    LANGUAGES_NAME_KEYS,
    REGIONS_BY_SHORTCODE,
    REGIONS_NAME_KEYS,
    TAG_REGEX,
    FSHandler,
)

# list of known compressed file MIME types
COMPRESSED_MIME_TYPES: Final = [
    "application/zip",
    "application/x-tar",
    "application/x-gzip",
    "application/x-7z-compressed",
    "application/x-bzip2",
]

# list of known file extensions that are compressed
COMPRESSED_FILE_EXTENSIONS = [
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".bz2",
]

FILE_READ_CHUNK_SIZE = 1024 * 8


class FSRom(TypedDict):
    multi: bool
    fs_name: str
    files: list[RomFile]


class FileHash(TypedDict):
    id: int
    crc_hash: str
    md5_hash: str
    sha1_hash: str


def is_compressed_file(file_path: str) -> bool:
    mime = magic.Magic(mime=True)
    file_type = mime.from_file(file_path)

    return file_type in COMPRESSED_MIME_TYPES or file_path.endswith(
        tuple(COMPRESSED_FILE_EXTENSIONS)
    )


def read_basic_file(file_path: Path) -> Iterator[bytes]:
    with open(file_path, "rb") as f:
        while chunk := f.read(FILE_READ_CHUNK_SIZE):
            yield chunk


def read_zip_file(file_path: Path) -> Iterator[bytes]:
    try:
        with zipfile.ZipFile(file_path, "r") as z:
            for file in z.namelist():
                with z.open(file, "r") as f:
                    while chunk := f.read(FILE_READ_CHUNK_SIZE):
                        yield chunk
    except zipfile.BadZipFile:
        for chunk in read_basic_file(file_path):
            yield chunk


def read_tar_file(
    file_path: Path, mode: Literal["r", "r:*", "r:", "r:gz", "r:bz2", "r:xz"] = "r"
) -> Iterator[bytes]:
    try:
        with tarfile.open(file_path, mode) as f:
            for member in f.getmembers():
                # Ignore directories and any other non-regular files
                if not member.isfile():
                    continue

                # Ignore metadata files created by macOS
                if member.name.startswith("._"):
                    continue

                with f.extractfile(member) as ef:  # type: ignore
                    while chunk := ef.read(FILE_READ_CHUNK_SIZE):
                        yield chunk
    except tarfile.ReadError:
        for chunk in read_basic_file(file_path):
            yield chunk


def read_gz_file(file_path: Path) -> Iterator[bytes]:
    return read_tar_file(file_path, "r:gz")


def process_7z_file(
    file_path: Path,
    fn_hash_update: Callable[[bytes | bytearray], None],
    fn_hash_read: Callable[[int | None], bytes],
) -> None:
    """Process a 7zip file and use the provided callables to update the calculated hashes.

    7zip files are special, as the py7zr library does not provide a similar interface to the
    other compression utils. Instead, we must use a factory to intercept the read and write
    operations of the 7zip file to calculate the hashes.

    Hashes end up being updated by reference in the provided callables, so they will include the
    final hash when this function returns.
    """

    try:
        factory = CallbackIOFactory(
            on_write=fn_hash_update,
            on_read=fn_hash_read,
        )
        # Provide a file handler to `SevenZipFile` instead of a file path to deactivate the
        # "parallel" mode in py7zr, which is needed to deterministically calculate the hashes, by
        # reading each included file in order, one by one.
        with open(file_path, "rb") as f:
            with py7zr.SevenZipFile(f, mode="r") as archive:
                archive.extractall(factory=factory)  # nosec B202
    except (
        Bad7zFile,
        DecompressionError,
        PasswordRequired,
        UnsupportedCompressionMethodError,
    ):
        for chunk in read_basic_file(file_path):
            fn_hash_update(chunk)


def read_bz2_file(file_path: Path) -> Iterator[bytes]:
    try:
        with bz2.BZ2File(file_path, "rb") as f:
            while chunk := f.read(FILE_READ_CHUNK_SIZE):
                yield chunk
    except EOFError:
        for chunk in read_basic_file(file_path):
            yield chunk


class FSRomsHandler(FSHandler):
    def __init__(self) -> None:
        pass

    def remove_from_fs(self, fs_path: str, fs_name: str) -> None:
        try:
            os.remove(f"{LIBRARY_BASE_PATH}/{fs_path}/{fs_name}")
        except IsADirectoryError:
            shutil.rmtree(f"{LIBRARY_BASE_PATH}/{fs_path}/{fs_name}")

    def parse_tags(self, fs_name: str) -> tuple:
        rev = ""
        regs = []
        langs = []
        other_tags = []
        tags = [tag[0] or tag[1] for tag in TAG_REGEX.findall(fs_name)]
        tags = [tag for subtags in tags for tag in subtags.split(",")]
        tags = [tag.strip() for tag in tags]

        for tag in tags:
            if tag.lower() in REGIONS_BY_SHORTCODE.keys():
                regs.append(REGIONS_BY_SHORTCODE[tag.lower()])
                continue

            if tag.lower() in REGIONS_NAME_KEYS:
                regs.append(tag)
                continue

            if tag.lower() in LANGUAGES_BY_SHORTCODE.keys():
                langs.append(LANGUAGES_BY_SHORTCODE[tag.lower()])
                continue

            if tag.lower() in LANGUAGES_NAME_KEYS:
                langs.append(tag)
                continue

            if "reg" in tag.lower():
                match = re.match(r"^reg[\s|-](.*)$", tag, re.IGNORECASE)
                if match:
                    regs.append(
                        REGIONS_BY_SHORTCODE[match.group(1).lower()]
                        if match.group(1).lower() in REGIONS_BY_SHORTCODE.keys()
                        else match.group(1)
                    )
                    continue

            if "rev" in tag.lower():
                match = re.match(r"^rev[\s|-](.*)$", tag, re.IGNORECASE)
                if match:
                    rev = match.group(1)
                    continue

            other_tags.append(tag)
        return regs, rev, langs, other_tags

    def _exclude_multi_roms(self, roms: list[str]) -> list[str]:
        excluded_names = cm.get_config().EXCLUDED_MULTI_FILES
        filtered_files: list = []

        for rom in roms:
            if rom in excluded_names:
                filtered_files.append(rom)

        return [f for f in roms if f not in filtered_files]

    def _build_rom_file(self, file_path: Path, file_name: str) -> RomFile:
        full_path = Path(file_path, file_name)

        return RomFile(
            file_name=file_name,
            file_path=file_path.name,
            file_size_bytes=os.stat(full_path).st_size,
            last_modified=os.path.getmtime(full_path),
        )

    def get_rom_files(self, rom: str, roms_path: str) -> list[RomFile]:
        rom_files: list[RomFile] = []

        # Check if rom is a multi-part rom
        if os.path.isdir(f"{roms_path}/{rom}"):
            multi_files = os.listdir(f"{roms_path}/{rom}")
            for file in self._exclude_files(multi_files, "multi_parts"):
                path = Path(roms_path, rom)
                rom_files.append(self._build_rom_file(path, file))
        else:
            path = Path(roms_path)
            rom_files.append(self._build_rom_file(path, rom))

        return rom_files

    def _calculate_rom_hashes(
        self,
        file_path: Path,
        rom_crc_c: int,
        rom_md5_h: Any,
        rom_sha1_h: Any,
    ) -> tuple[int, int, Any, Any, Any, Any]:
        mime = magic.Magic(mime=True)
        file_type = mime.from_file(file_path)
        extension = Path(file_path).suffix.lower()

        crc_c = 0
        md5_h = hashlib.md5(usedforsecurity=False)
        sha1_h = hashlib.sha1(usedforsecurity=False)

        def update_hashes(chunk: bytes | bytearray):
            md5_h.update(chunk)
            rom_md5_h.update(chunk)

            sha1_h.update(chunk)
            rom_sha1_h.update(chunk)

            nonlocal crc_c
            crc_c = binascii.crc32(chunk, crc_c)
            nonlocal rom_crc_c
            rom_crc_c = binascii.crc32(chunk, rom_crc_c)

        if extension == ".zip" or file_type == "application/zip":
            for chunk in read_zip_file(file_path):
                update_hashes(chunk)

        elif extension == ".tar" or file_type == "application/x-tar":
            for chunk in read_tar_file(file_path):
                update_hashes(chunk)

        elif extension == ".gz" or file_type == "application/x-gzip":
            for chunk in read_gz_file(file_path):
                update_hashes(chunk)

        elif extension == ".7z" or file_type == "application/x-7z-compressed":
            process_7z_file(
                file_path=file_path,
                fn_hash_update=update_hashes,
                fn_hash_read=lambda size: sha1_h.digest(),
            )

        elif extension == ".bz2" or file_type == "application/x-bzip2":
            for chunk in read_bz2_file(file_path):
                update_hashes(chunk)

        else:
            for chunk in read_basic_file(file_path):
                update_hashes(chunk)

        return crc_c, rom_crc_c, md5_h, rom_md5_h, sha1_h, rom_sha1_h

    def get_rom_hashes(self, rom: Rom) -> tuple[FileHash, list[FileHash]]:
        roms_file_path = f"{LIBRARY_BASE_PATH}/{rom.fs_path}"

        rom_crc_c = 0
        rom_md5_h = hashlib.md5(usedforsecurity=False)
        rom_sha1_h = hashlib.sha1(usedforsecurity=False)

        files = rom.files
        hashed_files = []

        for file in files:
            path = Path(roms_file_path, file.file_path, file.file_name)
            crc_c, rom_crc_c, md5_h, rom_md5_h, sha1_h, rom_sha1_h = (
                self._calculate_rom_hashes(path, rom_crc_c, rom_md5_h, rom_sha1_h)
            )
            hashed_files.append(
                FileHash(
                    id=file.id,
                    crc_hash=crc32_to_hex(crc_c),
                    md5_hash=md5_h.hexdigest(),
                    sha1_hash=sha1_h.hexdigest(),
                )
            )

        return (
            FileHash(
                id=rom.id,
                crc_hash=crc32_to_hex(rom_crc_c),
                md5_hash=rom_md5_h.hexdigest(),
                sha1_hash=rom_sha1_h.hexdigest(),
            ),
            hashed_files,
        )

    def get_roms(self, platform_fs_slug: str) -> list[FSRom]:
        """Gets all filesystem roms for a platform

        Args:
            platform: platform where roms belong
        Returns:
            list with all the filesystem roms for a platform found in the LIBRARY_BASE_PATH
        """
        roms_path = self.get_roms_fs_structure(platform_fs_slug)
        roms_file_path = f"{LIBRARY_BASE_PATH}/{roms_path}"

        try:
            fs_single_roms = [f for _, f in iter_files(roms_file_path)]
        except IndexError as exc:
            raise RomsNotFoundException(platform_fs_slug) from exc

        try:
            fs_multi_roms = [d for _, d in iter_directories(roms_file_path)]
        except IndexError as exc:
            raise RomsNotFoundException(platform_fs_slug) from exc

        fs_roms: list[dict] = [
            {"multi": False, "fs_name": rom}
            for rom in self._exclude_files(fs_single_roms, "single")
        ] + [
            {"multi": True, "fs_name": rom}
            for rom in self._exclude_multi_roms(fs_multi_roms)
        ]

        return sorted(
            [
                FSRom(
                    multi=rom["multi"],
                    fs_name=rom["fs_name"],
                    files=self.get_rom_files(rom["fs_name"], roms_file_path),
                )
                for rom in fs_roms
            ],
            key=lambda rom: rom["fs_name"],
        )

    def file_exists(self, fs_path: str, fs_name: str) -> bool:
        """Check if file exists in filesystem

        Args:
            path: path to file
            fs_name: name of file
        Returns
            True if file exists in filesystem else False
        """
        return bool(os.path.exists(f"{LIBRARY_BASE_PATH}/{fs_path}/{fs_name}"))

    def rename_fs_rom(self, old_name: str, new_name: str, fs_path: str) -> None:
        if new_name != old_name:
            if self.file_exists(fs_path=fs_path, fs_name=new_name):
                raise RomAlreadyExistsException(new_name)

            os.rename(
                f"{LIBRARY_BASE_PATH}/{fs_path}/{old_name}",
                f"{LIBRARY_BASE_PATH}/{fs_path}/{new_name}",
            )

    def build_upload_file_path(self, fs_slug: str) -> str:
        file_path = self.get_roms_fs_structure(fs_slug)
        return f"{LIBRARY_BASE_PATH}/{file_path}"
