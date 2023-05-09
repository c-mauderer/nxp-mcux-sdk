#!/usr/bin/env python3

""" Parse the cmake files from mcux_sdk and import the sources to RTEMS.
This script is a ugly hack and it might doesn't work with newer mcux_sdk
versions.

Provide a number of the cmake files in `devices/MIMXRT*/all_lib_device_*.cmake`
as parameter to this script. The file format is expected to be quite fixed.
Don't give anything else to the script. It won't work.
"""

import argparse
import logging
from enum import Enum
from pathlib import Path
import re
from slugify import slugify
import shutil
import yaml

class states(Enum):
    IDLE = 0
    ADD_MODULE_PATH = 1
    ADD_CFILES = 2
    ADD_HFILES = 3
    SKIP_TILL_ENDIF = 4

def find_file(name, paths):
    for p in paths:
        path = Path(p)
        f = path / f'{name}.cmake'
        if f.exists():
            return f.resolve(strict=True)
    return None

def get_path_from_line(line, cmake, rel_to = None):
    current_list_dir = str(Path(cmake.name).parent.resolve())
    path = line.strip().replace("${CMAKE_CURRENT_LIST_DIR}", current_list_dir)
    path = Path(path)
    if rel_to is not None:
        p = Path(path)
        path = p.relative_to(rel_to)
    return path

def parse_cmake(cmake, mcux_device, module_path = [], rel_to = None, stack = []):
    logger.info(f"Parse file: {cmake.name}")
    if rel_to is None:
        rel_to = Path(cmake.name).parent.resolve()
    mod_path = module_path.copy()
    state = states.IDLE
    cfiles = []
    hpaths = []

    # prevent loops
    if cmake.name in stack:
        logger.warning(f"Loop detected: {' -> '.join(stack)}")
        return cfiles, hpaths
    # Use a shallow copy!
    stack_work = stack[:] + [cmake.name]

    for line in cmake:
        logger.debug(f"state: {state}; file: {cmake.name}; line '{line.strip()}'")

        if state == states.IDLE:
            if "if(${MCUX_DEVICE} STREQUAL " in line:
                equal_to = re.search(r'"(.*)"', line).group(1)
                if equal_to == mcux_device:
                    logger.debug(f"MCUX matches {equal_to}")
                else:
                    logger.debug(f"MCUX ({mcux_device}) not equal to {equal_to}")
                    state = states.SKIP_TILL_ENDIF
            elif "endif()" in line:
                state = states.IDLE
            elif ("list" in line) and ("APPEND" in line) and ("CMAKE_MODULE_PATH" in line):
                state = states.ADD_MODULE_PATH
            elif ("target_sources" in line):
                state = states.ADD_CFILES
            elif ("target_include_directories" in line):
                state = states.ADD_HFILES
            elif ("include(" in line):
                # NOTE: This will also include commented lines. This is
                # necessary because NXP only added the includes as an example to
                # the all_lib_device_*.cmake files.
                to_include = re.search(r"\((.*)\)", line).group(1)
                included = find_file(to_include, mod_path)
                logger.debug(f"Search include file: {to_include}; found {included}")
                if included is None:
                    # log unexpected misses
                    if not (to_include.startswith("middleware") or \
                            to_include.startswith("CMSIS")):
                        logger.error(f"can't find {to_include}")
                elif "rtos" in to_include:
                    logger.info("Skip include for other RTOS: ${to_include}")
                elif "utility" in to_include:
                    logger.info("Skip utilities: ${to_include}")
                elif "caam" in to_include:
                    logger.info("Filter non working driver: ${to_include}")
                elif included.relative_to(rel_to).parts[0] == "drivers" or \
                        included.relative_to(rel_to).parts[0] == "devices":
                    # we are only interested in drivers
                    with open(included, "r") as f:
                        cf, hp = parse_cmake(f, mcux_device, mod_path, rel_to, stack_work)
                        cfiles += cf
                        hpaths += hp
                else:
                    logger.debug(f"Ignore included file: {to_include} found at {included}")

        elif state == states.SKIP_TILL_ENDIF:
            if "endif()" in line:
                state = states.IDLE

        elif state == states.ADD_MODULE_PATH:
            if ")" in line:
                state = states.IDLE
            else:
                path = rel_to / get_path_from_line(line, cmake, rel_to)
                logger.info(f"Appended path {str(path)}")
                mod_path += [path]

        elif state == states.ADD_CFILES:
            if ")" in line:
                state = states.IDLE
            else:
                cf = get_path_from_line(line, cmake, rel_to)
                logger.debug(f"Appended c-file {str(cf)}")
                cfiles += [cf]

        elif state == states.ADD_HFILES:
            if ")" in line:
                state = states.IDLE
            else:
                hp = get_path_from_line(line, cmake, rel_to)
                logger.debug(f"Appended header path {str(hp)}")
                hpaths += [hp]

    logger.info(f"Parsing file done: {cmake.name}")
    logger.debug(f"  Found {cfiles}, {hpaths}")
    return cfiles, hpaths

class mcux_device:
    name = None
    cfiles = []
    hpaths = []

    def __init__(self, name, cfiles = [], hpaths = []):
        self.name = name
        self.cfiles = cfiles
        self.hpaths = hpaths

    def __str__(self):
        cfiles = ', '.join([str(cf) for cf in self.cfiles])
        hpaths = ', '.join([str(hp) for hp in self.hpaths])
        return f"MCUX {self.name}: cfiles: ({cfiles}), hpaths: ({hpaths})"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
            "cmake",
            help="Path to all_devices.cmake",
            type=argparse.FileType('r'),
        )
    parser.add_argument(
            "MCUX_DEVICE",
            help="Values for MCUX_DEVICE that should be used",
            type=str,
            nargs='+',
        )
    parser.add_argument(
            "-l", "--loglevel",
            help = "Define log level.",
            type = str.upper,
            choices = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
            default = 'INFO',
        )
    parser.add_argument(
            "-r", "--rtems",
            help = "RTEMS base path",
            required = True,
        )
    parser.add_argument(
            "-c", "--copy",
            action = 'store_true',
        )
    parser.add_argument(
            "-t", "--collect",
            action = 'store_true',
        )

    args = parser.parse_args()

    numeric_loglevel = getattr(logging, args.loglevel)
    logging.basicConfig(level = numeric_loglevel)
    logger = logging.getLogger("mcux")
    logger.debug(f"Started with the following options: {args}")

    devices = []

    # get c-files and header paths from mcux SDK
    for mcux in args.MCUX_DEVICE:
        args.cmake.seek(0)
        cf, hp = parse_cmake(args.cmake, mcux)
        devices += [mcux_device(mcux, cf, hp)]

    if args.collect:
        # find files common in multiple devices
        common_cfiles = set.intersection(*[set(d.cfiles) for d in devices])
        common_hpaths = set.intersection(*[set(d.hpaths) for d in devices])
        common = mcux_device("common",
                sorted(list(common_cfiles)), sorted(list(common_hpaths)))

        # remove common parts from devices
        for d in devices:
            d.cfiles = sorted(list(set(d.cfiles) - common_cfiles))
            d.hpaths = sorted(list(set(d.hpaths) - common_hpaths))

        devices += [common]
    else:
        # Only sort files and make sure to have them only once
        for d in devices:
            d.cfiles = sorted(list(set(d.cfiles)))
            d.hpaths = sorted(list(set(d.hpaths)))

    for d in devices:
        logger.info(str(d))

    # Now for the interesting part: Copying sources and generating yml files
    mcux_path = Path(args.cmake.name).parent.resolve()
    rtems_path = Path(args.rtems)
    rtems_mcux_path = Path("bsps/arm/imxrt/mcux-sdk")
    for d in devices:
        yml_file = Path(args.rtems) / "spec" / "build" / "bsps" / "arm" / \
                "imxrt" / f"obj-{slugify(d.name)}.yml"
        yml = {
                "SPDX-License-Identifier": "CC-BY-SA-4.0 OR BSD-2-Clause",
                "build-type": "objects",
                "cflags": [],
                "copyrights":
                  ["Copyright (C) 2023 embedded brains GmbH (http://www.embedded-brains.de)"],
                "cppflags": [],
                "cxxflags": [],
                "ldflags": [],
                "enabled-by": True,
                "includes": [],
                "install": [
                    {
                        "destination": "${BSP_INCLUDEDIR}",
                        "source": [],
                    }
                ],
                "links": [
                    {
                        "role": "build-dependency",
                        "uid": "grp",
                    }
                ],
                "source": [],
                "type": "build",
                "use-after": [],
                "use-before": [],
            }

        # Process source files
        for cf in d.cfiles:
            logger.info(f"Processing {cf}")
            cf_rtems = rtems_mcux_path / cf
            if args.copy:
                dst = rtems_path / cf_rtems
                src = mcux_path / cf
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            yml["source"] += [str(cf_rtems)]

        # Process header files
        for hp in d.hpaths:
            logger.info(f"Processing {hp}")
            hp_rtems = rtems_mcux_path / hp
            yml["includes"] += [str(hp_rtems)]
            dst_path = rtems_path / hp_rtems
            src_path = mcux_path / hp
            if args.copy:
                dst_path.mkdir(parents=True, exist_ok=True)

            for h_src in sorted(src_path.glob("*.h")):
                h_rel = h_src.relative_to(mcux_path)
                h_dst = rtems_mcux_path / h_rel
                yml["install"][0]["source"] += [str(h_dst)]
                if args.copy:
                    (rtems_path / h_dst).write_bytes(h_src.read_bytes())

        # Write yml
        with open(yml_file, 'w') as out:
            yaml.dump(yml, out)
        logger.info(f"Written to {yml_file}")
