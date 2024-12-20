#!/usr/bin/env python3
#
# Copyright (C) 2024 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import os
import subprocess
import sys
import tempfile
from typing import List, Optional

import context
from llvm_android.utils import check_tools, extract_tarball

prefix = "gs://android-llvm-kokoro-ci-artifacts/prod/android-llvm/linux-tot/continuous/"


def parse_args(sys_argv: Optional[List[str]]):
    """Parse the command line arguments."""

    parser = argparse.ArgumentParser(description="Fetch prebuilts from kokoro.")

    ref = parser.add_mutually_exclusive_group(required=True)
    ref.add_argument(
        "--sha",
        type=str,
        nargs="?",
        help="the build sha of llvm-project of one Kokoro build in Test fusion",
    )

    ref.add_argument(
        "--build_id",
        type=str,
        nargs="?",
        help=(
            "the build id in"
            " https://pantheon.corp.google.com/storage/browser/android-llvm-kokoro-ci-artifacts"
            ", or \"latest\" for the most recent build."
        ),
    )

    parser.add_argument(
        "path",
        type=str,
        nargs=1,
        help="Path to save the extracted Clang (e.g. ANDROID_TOP/prebuilts/clang/linux-x86/)",
    )
    return parser.parse_args(sys_argv)


def get_url(build_id: str):
    suffix = "/**.tar.xz"
    url = prefix + build_id + suffix
    return url


def fetch_prebuilts(build_id: str, path: str):
    gsutil_url = get_url(build_id)
    with tempfile.TemporaryDirectory() as td:
        cmd = ["gsutil", "cp", gsutil_url, td]
        result = subprocess.run(cmd, stderr=subprocess.PIPE)
        if result.returncode > 0:
            err_string = str(result.stderr, encoding="utf-8")
            if "CommandException: No URLs matched" in err_string:
                print(f"Build {build_id} failed to build.")
            else:
                print(err_string)
        else:
            print(f"Download build {build_id} successful!")

            # extract the toolchain
            tar = os.path.abspath(td) + "/" + os.listdir(td)[0]
            extract_tarball(path, tar)
            return True
    return False


def check_valid_build(build_id: str):
    url = prefix + build_id + "/"
    output = subprocess.check_output(["gsutil", "ls", "-L", prefix])
    if url not in str(output):
        err_msg = build_id + " doesn't exist. Please pick a valid build id."
        raise Exception(err_msg)


def check_valid_path(path: str):
    if not os.path.exists(path):
        err_msg = path + " doesn't exist. Please pass a valid path."
        raise Exception(err_msg)


def stubby_call(request: str):
    cmd = [
        "stubby",
        "call",
        "blade:kokoro-api",
        "KokoroApi.GetBuildStatus",
        request,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def get_build_number(sha: str):
    """Find the build number that contains the SHA.

    Find if SHA is the latest change in any build.
    If not, find if the SHA is included in the latest build and get the
    latest build number so we can walk the kokoro builds.
    Keep looking at older builds until we find a match or exhaust all builds.

    Args:
        sha: the SHA of the llvm-project change.

    Returns:
        The build number that contains the SHA.
    """
    build_number = None

    request = f"""
        full_job_name: "android-llvm/linux-tot/continuous"
        multi_scm_revision: {{
          git_on_borg_scm_revision: {{
            name: "toolchain/llvm-project",
            branch: "main",
            sha1: "{sha}"
          }}
        }}
    """
    response = stubby_call(request)
    if response.returncode == 0:
        # The sha is from the latest change in any build.
        data = response.stdout
        for line in data.splitlines():
            if line.startswith("build_number:"):
                build_number = line.split()[-1]
                return build_number

    found = False
    request = """
        full_job_name: "android-llvm/linux-tot/continuous"
        multi_scm_revision: {
          git_on_borg_scm_revision: {
              name: "toolchain/llvm-project",
              branch: "main"
          }
        }
    """

    while not found:
        response = stubby_call(request)
        if response.returncode != 0:
            print(
                f"No build number for {sha} matched!",
                file=sys.stderr,
            )
            sys.exit(1)

        # Parse the response.
        data = response.stdout
        for line in data.splitlines():
            line = line.strip()
            if line == f'id: "{sha}"':
                found = True
            if line.startswith("build_number:"):
                build_number = line.split()[-1]
                break

        # The current build doesn't have the SHA.
        # Try to find the SHA in the next older build.
        if not found:
            build_number = str(int(build_number) - 1)
            request = f"""
                build_number: {build_number}
                full_job_name: "android-llvm/linux-tot/continuous"
                multi_scm_revision: {{
                git_on_borg_scm_revision: {{
                    name: "toolchain/llvm-project",
                    branch: "main"
                }}
                }}
            """

    return build_number


def get_latest_build_id():
    request = """
        full_job_name: "android-llvm/linux-tot/continuous"
        multi_scm_revision: {
          git_on_borg_scm_revision: {
              name: "toolchain/llvm-project",
              branch: "main"
          }
        }
    """
    response = stubby_call(request)
    data = response.stdout
    for line in data.splitlines():
        if line.startswith("build_number:"):
            return line.split()[-1]
    return "-1"


def main(sys_argv: List[str]):
    args_output = parse_args(sys_argv)

    check_tools(args_output.sha)

    path = args_output.path[0]
    check_valid_path(path)

    if args_output.sha:
        build_id = get_build_number(args_output.sha)
    elif args_output.build_id == "latest":
        build_id = get_latest_build_id()
    else:
        build_id = args_output.build_id

    check_valid_build(build_id)

    fetch_prebuilts(build_id, path)

    return 0


if __name__ == "__main__":
    main(sys.argv[1:])
