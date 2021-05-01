#!/usr/bin/env python3

import asyncio
import functools
import gzip
import hashlib
import logging
import os
import struct
import sys
from contextlib import contextmanager
from shutil import rmtree
from tempfile import mkdtemp
from time import time
from typing import List, Tuple

import docker as docker_py
import guestfs
import humanize
import typer

log = logging.getLogger('build')
logging.basicConfig(level='INFO')

BOOT_PART_SIZE = 100 * 1024**2
DATA_PART_SIZE = 100 * 1024**2
OS_PART_SIZE_OVERHEAD = 1.5
SECTOR_SIZE = 512

GFS_DEVICE = '/dev/sda'
RPI_DEVICE = '/dev/mmcblk0p'
MBR_IDS = {
    'vfat': 0xc,
    'fat32': 0xc,
    'exfat': 0x7
}

@contextmanager
def temp_dir():
    tmp = mkdtemp()
    try:
        yield tmp
    finally:
        rmtree(tmp)

def run_async(f):
    @functools.wraps(f)
    def wrap(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrap

def periodic(n):
    """Decorator. Only run if not run within last n seconds."""
    def decorate(f):
        last_run = 0
        @functools.wraps(f)
        def wrap(*args, **kwargs):
            nonlocal last_run
            now = time()
            if now - last_run >= n:
                f(*args, **kwargs)
                last_run = now
        return wrap
    return decorate

def part_device(n):
    return GFS_DEVICE + str(n)

def get_partuuid(image_file, part_num):
    # Using partuuid instead of uuid is not ideal with MBR, but apparently the latter requires initrd
    # There doesn't seem to be a way to do this with guestfs
    SIGNATURE_OFFSET = 440
    with open(image_file, 'rb') as f:
        f.seek(SIGNATURE_OFFSET)
        id = struct.unpack('<L', f.read(4))[0]
        return f'{id:08x}-{part_num:02d}'

@contextmanager
def create_empty_image(image_file, size):
    try:
        os.remove(image_file)
    except:
        pass
    gfs = guestfs.GuestFS(python_return_dict=True)
    image_size = int((size / SECTOR_SIZE) + 1) * SECTOR_SIZE
    gfs.disk_create(image_file, 'raw', image_size)
    gfs.add_drive(image_file, readonly=False)
    gfs.launch()
    try:
        yield gfs
    finally:
        gfs.shutdown()

@contextmanager
def create_image_file(image_file, fs, size):
    with create_empty_image(image_file, size) as gfs:
        gfs.mkfs(fs, GFS_DEVICE)
        yield gfs

@contextmanager
def create_disk_image_file(image_file, *partitions: List[Tuple[int, str, str]]):
    start_size = 1024**2
    image_size = start_size + sum(( size for size, _, _ in partitions ))
    log.debug(f'image_size={image_size}')
    with create_empty_image(image_file, image_size) as gfs:
        assert gfs.list_devices() == [GFS_DEVICE]
        log.info('Partitioning image...')
        start = int(start_size / SECTOR_SIZE)
        gfs.part_init(GFS_DEVICE, 'mbr')
        for n, part in enumerate(partitions):
            num = n + 1
            size, format, label = part
            end = start + int(size / SECTOR_SIZE) - 1
            log.debug(f'part_add {start}-{end}')
            gfs.part_add(GFS_DEVICE, 'p', start, end)
            gfs.part_set_mbr_id(GFS_DEVICE, num, MBR_IDS.get(format, 0x83))
            gfs.mkfs(format, part_device(num), label=label)
            start = end + 1
        yield gfs

async def export_docker_image(image_name, out_tar_file):
    def _export():
        log.debug('Export thread running')
        written = 0

        @periodic(5)
        def log_progress():
            log.info(f' {humanize.naturalsize(written, format="%.2f")} exported')

        docker = docker_py.from_env()
        container = docker.containers.create(image_name)
        log.debug(f'Container: {container.id}')
        try:
            with open(out_tar_file, 'wb') as f:
                for chunk in container.export():
                    f.write(chunk)
                    written += len(chunk)
                    log_progress()
        finally:
            container.remove()
    await asyncio.to_thread(_export)

async def gzip_file(input_file, output_gzip):

    @periodic(5)
    def log_progress(written):
        log.info(f' {output_gzip}: {humanize.naturalsize(written, format="%.2f")} compressed')

    with gzip.open(output_gzip, 'wb') as gz_f:
        with open(input_file, 'rb') as in_f:
            written = 0
            while chunk := in_f.read(64 * 1024):
                await asyncio.sleep(0)
                gz_f.write(chunk)
                await asyncio.sleep(0)
                written += len(chunk)
                log_progress(written)

async def export_docker_to_mount(gfs, docker_image, mount_point):
    with temp_dir() as tmp:
        fifo = os.path.join(tmp, 'fifo')
        os.mkfifo(fifo)
        export_task = asyncio.create_task(export_docker_image(docker_image, fifo))
        import_task = asyncio.create_task(asyncio.to_thread(lambda: gfs.tar_in(fifo, mount_point)))
        for t in asyncio.as_completed((export_task, import_task)):
            await t

def update_fstab(gfs, root_uuid, boot_uuid, data_uuid):
    log.info('Updating fstab...')
    FSTAB_PATH = '/etc/fstab'
    new_fstab = ''
    for line in gfs.read_lines(FSTAB_PATH):
        parts = line.split()
        mount = ''
        remainder = ''
        if len(parts) >= 2:
            mount = parts[1]
            remainder = ' '.join(parts[1:]) + '\n'
        if mount == '/':
            new_fstab += f'PARTUUID={root_uuid} {remainder}'
        elif mount == '/boot':
            new_fstab += f'PARTUUID={boot_uuid} {remainder}'
        else:
            new_fstab += line + '\n'
    if data_uuid:
        new_fstab += f'PARTUUID={data_uuid} /data vfat defaults 0 2\n'
    gfs.write(FSTAB_PATH, new_fstab)                    

async def sha256sum(input, output, name):
    COPY_BUFSIZE = 64 * 1024
    sha = hashlib.sha256()
    with open(input, 'rb') as in_f:
        while True:
            chunk = in_f.read(COPY_BUFSIZE)
            await asyncio.sleep(0)
            if not chunk:
                break
            sha.update(chunk)
            await asyncio.sleep(0)
    with open(output, 'w') as out_f:
        out_f.write(f'{sha.hexdigest()}  {name}\n')

async def convert_docker_to_rpi_image(docker_image: str, full_disk: bool, b_part: bool, data_part: bool, output_gzip: str, output_boot: str, hash_files: bool):
    docker_api = docker_py.api.APIClient()
    try:
        os_part_size = int(docker_api.inspect_image(docker_image)['Size'] * OS_PART_SIZE_OVERHEAD)
    except docker_py.errors.ImageNotFound:
        log.critical(f'{docker_image} image not found')
        sys.exit(1)
    log.debug(f'os_part_size {humanize.naturalsize(os_part_size)}')
    
    log.info('Creating output image...')
    USE_GZ = output_gzip.endswith('.gz')
    with temp_dir() as tmp:
        if USE_GZ:
            image_file = os.path.join(tmp, 'image')
            if output_boot:
                boot_tar = os.path.join(tmp, 'boot')
        else:
            image_file = output_gzip
            if output_boot:
                boot_tar = output_boot

        if full_disk:
            partitions = [
                # size              format      label
                (BOOT_PART_SIZE,    'vfat',    'boot'),
                (os_part_size,      'ext4',    'os_a'),
            ]
            if b_part:
                partitions.append((10 * 1024**2,      'ext4',    'os_b')) # placeholder size
            if data_part:
                partitions.append((DATA_PART_SIZE,    'vfat',    'data'))
            with create_disk_image_file(image_file, *partitions) as gfs:
                boot_uuid = get_partuuid(image_file, 1)
                root_uuid = get_partuuid(image_file, 2)
                data_uuid = get_partuuid(image_file, 4) if data_part else None                
                log.info(f'PARTUUIDs: boot {boot_uuid}, root {root_uuid}, data {data_uuid}')

                log.info('Copying from Docker image...')
                gfs.mount(part_device(2), '/')
                await export_docker_to_mount(gfs, docker_image, '/')

                # Flag data directory for the config script to find it
                gfs.mkdir('/data')
                gfs.mount(part_device(4), '/data')
                gfs.write('/data/.configure_me', '')

                update_fstab(gfs, root_uuid, boot_uuid, data_uuid)

                # Copy boot files
                log.info('Preparing boot partition...')
                BOOT_DIR = '/tmp/boot' 
                gfs.mkdir(BOOT_DIR)
                boot_dev = part_device(1)
                try:
                    gfs.mount(boot_dev, BOOT_DIR)
                    boot_files = gfs.glob_expand('/boot/*')
                    for src in boot_files:
                        gfs.cp_r(src, BOOT_DIR)
                        gfs.rm_rf(src)
                    # Update root uuid in boot cmdline
                    cmdline_path = BOOT_DIR + '/cmdline.txt'
                    cmdline: str = gfs.read_file(cmdline_path).decode().split()
                    for n, part in enumerate(cmdline):
                        if part.startswith('root='):
                            cmdline[n] = f'root=PARTUUID={root_uuid}'
                            break
                    gfs.write(cmdline_path, ' '.join(cmdline).encode())

                    if output_boot:
                        gfs.tar_out(BOOT_DIR, boot_tar)
                finally:
                    gfs.umount(boot_dev)
                    gfs.rm_rf(BOOT_DIR)

        else:
            with create_image_file(image_file, 'ext4', os_part_size) as gfs:
                gfs.mount(GFS_DEVICE, '/')
                await export_docker_to_mount(gfs, docker_image, '/')
                if output_boot:
                    gfs.tar_out('/boot', boot_tar)
                gfs.rm_rf('/boot')
                gfs.mkdir('/boot')
                # There is no MBR, these will be psuedo ids that need to be updated by the flasher
                boot_uuid = get_partuuid(image_file, 1)
                root_uuid = get_partuuid(image_file, 2)
                data_uuid = get_partuuid(image_file, 4) if data_part else None
                update_fstab(gfs, root_uuid, boot_uuid, data_uuid)

        if hash_files:
            log.info('Hashing files...')
            HASH_SUFFIX = '.sha256'
            def get_hash_params(f_name):
                path = f_name.removesuffix('.gz')
                return path + HASH_SUFFIX, os.path.basename(path)
            os_task = asyncio.create_task(sha256sum(image_file, *get_hash_params(output_gzip)))
            if output_boot:
                await sha256sum(boot_tar, *get_hash_params(output_boot))
            await os_task

        if USE_GZ:
            log.info('Compressing output...')
            os_task = asyncio.create_task(gzip_file(image_file, output_gzip))
            if output_boot:
                await gzip_file(boot_tar, output_boot)
            await os_task

    log.info('Done')

def build_docker_image(dockerfile='Dockerfile'):
    log.info(f'Building docker image from {dockerfile}')
    docker = docker_py.APIClient()
    image_id = None
    for line in docker.build(path='.', rm=True, dockerfile=dockerfile, decode=True):
        if 'stream' in line:
            log_line = line['stream'].strip()
            if log_line:
                log.info(log_line)
        elif 'aux' in line:
            aux = line['aux']
            if 'ID' in aux:
                image_id = aux['ID']
    if not image_id:
        raise Exception('Docker build failed')
    return image_id

@run_async
async def build(
    output_image: str = typer.Argument(..., help='Output image file (suffix with .gz to compress)'),
    output_boot_files: str = typer.Argument(None, help='Output archive containing boot files (suffix with .gz to compress)'),
    docker_image: str = typer.Option(None, help='Name of source docker image. Takes precedence over docker-file.'),
    docker_file: str = typer.Option('./Dockerfile', help='Path to Dockerfile to be built as source image'),
    partitions: bool = typer.Option(True, is_flag=True, help='Create a full flashable image with partitions, else just an OS FS image.'),
    b_partition: bool = typer.Option(False, is_flag=True, help='Include a second (blank) OS partition in the image (for a/b flashing)'),
    data_partition: bool = typer.Option(False, is_flag=True, help='Include a FAT partition for data'),
    hashes: bool = typer.Option(True, is_flag=True, help='Also produce sha256sum files (these are pre-compression hashes).')
):
    """Convert a Docker image to a Raspberry Pi image."""
    if not docker_image:
        docker_image = build_docker_image(docker_file)
    await convert_docker_to_rpi_image(docker_image, partitions, b_partition, data_partition, output_image, output_boot_files, hashes)

app = typer.Typer(add_completion=False)
app.command()(build)
app()
