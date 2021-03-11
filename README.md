# rpi-docker-build

A Python script which converts a Docker image (or Dockerfile) to a Raspberry Pi image.

The Docker image should itself be based on a proper Raspberry Pi image, for example [jonoh/raspberry-pi-os](https://hub.docker.com/r/jonoh/raspberry-pi-os). In particular, the Docker image should be a proper ARM image with a /boot containing the RPi kernel and standard boot files.

## Dependencies

The script needs a Linux system with libguestfs and Docker installed. If that's you, install the local dependencies with:
```
poetry install
```
Or you can run in Docker (see below).

## Usage

Ensure you run within the virtualenv (such as with `poetry run ./build.py`).

Alternatively, run in Docker with `docker-compose run build`.
A pre-built image is available at `jonoh/rpi-docker-build`, you can modify `docker-compose.yml` if you'd like to use this instead of building it yourself.

```
Usage: build.py [OPTIONS] OUTPUT_IMAGE [OUTPUT_BOOT_FILES]

  Convert a Docker image to a Raspberry Pi image.

Arguments:
  OUTPUT_IMAGE         Output image file (suffix with .gz to compress)
                       [required]

  [OUTPUT_BOOT_FILES]  Output archive containing boot files (suffix with .gz
                       to compress)


Options:
  --docker-image TEXT             Name of source docker image. Takes
                                  precedence over docker-file.

  --docker-file TEXT              Path to Dockerfile to be built as source
                                  image  [default: ./Dockerfile]

  --partitions / --no-partitions  Create a full flashable image with
                                  partitions, else just an OS FS image.
                                  [default: True]

  --b-partition / --no-b-partition
                                  Include a second (blank) OS partition in the
                                  image (for a/b flashing)  [default: False]

  --data-partition / --no-data-partition
                                  Include a FAT partition for data  [default:
                                  False]

  --help                          Show this message and exit.
```

